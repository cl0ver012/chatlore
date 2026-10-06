"""REST API over the library, with streaming chat.

`chatlore serve` runs it. Everything the command line shows is here too:
search, conversations, entities, topics, a drawable part of the graph, and
chat. Chat streams server-sent events: the sources first, then the answer as it
is written, then which sources it cited. The web interface at / is built on
these endpoints.

The MCP server is served at /mcp too, over streamable HTTP, for assistants
that connect to a URL instead of starting ``chatlore mcp``.

The web interface can also upload an export, which is imported, embedded, and
read into the knowledge graph in the background, and download the library as
an archive or Markdown. On a public server that takes uploads, each visitor who
uploads gets a private library of their own that is deleted after a while
(``chatlore.spaces``); everyone else sees the server's library.

Each request opens the store and closes it again, so requests never share a
database connection across threads. The server listens on 127.0.0.1 unless told
otherwise, because the library is private. A public server, such as the hosted
demo, limits how many questions reach the language model, since each one is paid
for with the host's key.
"""

from __future__ import annotations

import functools
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import unquote

import anyio
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from chatlore import __version__
from chatlore.archive import export_archive, export_markdown
from chatlore.chat import MAX_SOURCES, ChatLimits, Context, Source, answer, cited, retrieve
from chatlore.embeddings import Embedder, EmbeddingError, make_embedder, normalise
from chatlore.explore import NotFoundError, Window, conversation_entities, explore, timeline
from chatlore.facts import facts_about, find_facts
from chatlore.imports import Imports, safe_relative
from chatlore.library import Library
from chatlore.llm import LLMError, make_llm
from chatlore.mcp_server import create_server
from chatlore.paths import default_home
from chatlore.search import hybrid_search
from chatlore.spaces import COOKIE, Space, Spaces
from chatlore.store import Edge, EdgeType, GraphStore, Label, Node, open_store

WEB = Path(__file__).parent / "web"
"""The web interface's files, served at /."""


class _WebFiles(StaticFiles):
    """The interface's files, which the browser checks again on every load.

    Browsers otherwise keep an old copy for a while, so an upgrade would show the
    previous interface. Checking costs a "not modified" reply when nothing changed.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_HOUR = 3600.0
_MAX_VISITORS = 10_000


class _Allowance:
    """Counts questions against ``ChatLimits``. Shared by the request threads."""

    def __init__(self, limits: ChatLimits, clock: Callable[[], float] = time.time) -> None:
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._visitors: dict[str, deque[float]] = {}
        self._day = ""
        self._today = 0

    def take(self, visitor: str) -> str | None:
        """Count a question from ``visitor``, or say why it cannot be asked now."""
        now = self._clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self._lock:
            if day != self._day:
                self._day, self._today = day, 0
            asked = self._visitors.setdefault(visitor, deque())
            while asked and asked[0] <= now - _HOUR:
                asked.popleft()
            if len(asked) >= self.limits.per_visitor_hour:
                return (
                    f"This demo answers {self.limits.per_visitor_hour} questions an hour for "
                    "each visitor. Try again later, or install ChatLore to ask your own "
                    "conversations: pip install chatlore"
                )
            if self._today >= self.limits.per_day:
                return (
                    "This demo has answered all the questions it can today. Try again "
                    "tomorrow, or install ChatLore to ask your own conversations: "
                    "pip install chatlore"
                )
            asked.append(now)
            self._today += 1
            if len(self._visitors) > _MAX_VISITORS:
                self._visitors = {
                    key: times
                    for key, times in self._visitors.items()
                    if times and times[-1] > now - _HOUR
                }
        return None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    sources: int = Field(default=MAX_SOURCES, ge=1, le=20)


def _source(source: Source) -> dict[str, Any]:
    return {
        "number": source.number,
        "title": source.title,
        "source": source.source,
        "date": source.date,
        "role": source.role,
        "conversation_id": source.conversation_id,
        "message_id": source.message_id,
        "text": source.text,
    }


def _entity(node: Node) -> dict[str, Any]:
    props = node.props
    return {
        "id": node.id,
        "name": props.get("name"),
        "type": props.get("type"),
        "summary": props.get("summary"),
        "mentions": props.get("mentions"),
    }


def _topic(node: Node, entities: int | None = None) -> dict[str, Any]:
    props = node.props
    return {
        "id": node.id,
        "title": props.get("title"),
        "summary": props.get("summary"),
        "findings": props.get("findings", []),
        "size": props.get("size"),
        "entities": props.get("entities", [])[: entities or None],
    }


SEARCH_WAIT = 2.0
"""Seconds a search waits for the embedding model before answering by words alone."""

MAX_UPLOAD = 200_000_000
"""Bytes an upload may have unless the server says otherwise."""
REQUEST_HEADER = "X-ChatLore"
"""Sent by the web interface with every change. Another website cannot send it
without the browser first asking this server, which does not allow it, so it
cannot upload or delete through a visitor's browser."""
_SWEEP_SECONDS = 600
MAX_BATCH_FILES = 20_000
"""Files one upload may hold."""
_BATCH = re.compile(r"[0-9a-f]{32}")
_STALE_BATCH_SECONDS = 24 * 3600

# The visitor's own library, when the request carries a token for one.
_visitor: ContextVar[Space | None] = ContextVar("chatlore_visitor", default=None)


class _Visitors:
    """Serve each request from its visitor's own library, when it has one."""

    def __init__(self, app: ASGIApp, spaces: Spaces) -> None:
        self.app = app
        self.spaces = spaces

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        space = None
        if scope["type"] == "http":
            space = self.spaces.find(_cookie(scope, COOKIE))
        reset = _visitor.set(space)
        try:
            await self.app(scope, receive, send)
        finally:
            _visitor.reset(reset)


def _cookie(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            cookies: SimpleCookie = SimpleCookie()
            try:
                cookies.load(value.decode("latin-1"))
            except Exception:  # a malformed cookie header is no token
                return None
            if name in cookies:
                return cookies[name].value
    return None


def _library_cookie(request: Request, token: str, max_age: int) -> str:
    """The ``Set-Cookie`` value that gives the visitor's browser its library token.

    Over HTTPS the cookie is also sent inside another site's frame, as when a
    Hugging Face Space shows the demo on its page, and ``Partitioned`` keeps it
    apart for each site that frames it. Every change still needs the
    ``X-ChatLore`` header, which another site cannot send.
    """
    if request.url.scheme == "https":
        attributes = "SameSite=None; Secure; Partitioned"
    else:
        attributes = "SameSite=Lax"
    return f"{COOKIE}={token}; Max-Age={max_age}; Path=/; HttpOnly; {attributes}"


def _forget_stale_batches(uploads: Path) -> None:
    """Delete batches that were uploaded but never imported, a day on."""
    if not uploads.exists():
        return
    now = time.time()
    for folder in uploads.iterdir():
        try:
            stale = folder.is_dir() and now - folder.stat().st_mtime > _STALE_BATCH_SECONDS
        except OSError:
            continue
        if stale:
            shutil.rmtree(folder, ignore_errors=True)


def _only_file(folder: Path) -> str:
    return next((path.name for path in folder.rglob("*") if path.is_file()), "1 file")


def _changes_allowed(request: Request) -> None:
    if request.headers.get(REQUEST_HEADER) != "1":
        raise HTTPException(403, f"changes need the {REQUEST_HEADER} header")


def create_app(
    home: Path | None = None,
    *,
    public: bool = False,
    limits: ChatLimits | None = None,
    spaces: Spaces | None = None,
    max_upload: int = MAX_UPLOAD,
    extract_limit: int | None = None,
) -> FastAPI:
    """The API over the library in ``home``, or the default ChatLore home.

    A ``public`` app says so at /health, which makes the web interface show a
    demo note, and accepts MCP requests for any host name, since it is meant to
    be reached from the internet. Otherwise /mcp only answers requests addressed
    to this machine, which keeps other websites from reaching it through the
    browser. ``limits`` caps the questions sent to the language model.

    Uploads go into the library on a private server. A public one takes them
    only with ``spaces``, and puts each visitor's in a library of their own.
    ``extract_limit`` caps how many passages of an upload the model reads.
    """
    library = home or default_home()
    uploads = not public or spaces is not None
    assistants = create_server(library)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False) if public else None
    # Stateless, with plain JSON replies: any worker can answer any request, and
    # no tool streams its result.
    mcp_app = assistants.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security
    )

    embedders: list[Embedder] = []
    loading = threading.Lock()
    warmed = threading.Event()

    def embedder() -> Embedder:
        """One embedding model for the whole server, loaded on first use."""
        with loading:
            if not embedders:
                embedders.append(make_embedder(library))
            return embedders[0]

    def warm_up() -> None:
        """Load the embedding model before the first search needs it. The first time
        on a machine that downloads it, which can take minutes on a slow connection."""
        try:
            embedder().embed_query("warm up")
        except EmbeddingError:
            pass  # a search that needs it reports the error
        finally:
            warmed.set()

    # make_llm is looked up when an import needs it, so it can be replaced in tests.
    imports = Imports(embedder, lambda: make_llm(), extract_limit=extract_limit)

    def sweep() -> None:
        """Delete the visitors' libraries whose time is up."""
        if spaces is None:
            return
        for space in spaces.expired():
            imports.cancel(space.home, then=functools.partial(_forget, space))

    def _forget(space: Space) -> None:
        assert spaces is not None
        spaces.delete(space)
        imports.forget(space.home)

    async def sweeping() -> None:
        while True:
            await anyio.to_thread.run_sync(sweep)
            await anyio.sleep(_SWEEP_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with open_store(library) as graph:
            embedded = graph.count_embeddings() > 0
        if embedded or spaces is not None:
            threading.Thread(target=warm_up, name="chatlore-warm-up", daemon=True).start()
        else:
            warmed.set()
        async with anyio.create_task_group() as tasks:
            if spaces is not None:
                tasks.start_soon(sweeping)
            async with assistants.session_manager.run():
                yield
            tasks.cancel_scope.cancel()
        await anyio.to_thread.run_sync(imports.shutdown)

    app = FastAPI(
        title="ChatLore",
        version=__version__,
        description="All your AI conversations, one graph, one chat.",
        lifespan=lifespan,
    )
    allowance = _Allowance(limits) if limits is not None else None
    if spaces is not None:
        app.add_middleware(_Visitors, spaces=spaces)

    def store() -> GraphStore:
        space = _visitor.get()
        return space.open_store() if space is not None else open_store(library)

    def current_home() -> Path:
        space = _visitor.get()
        return space.home if space is not None else library

    def embed(text: str, graph: GraphStore, wait: float | None = None) -> list[float] | None:
        """The query's embedding, or None when the library has no embeddings.

        With ``wait``, also None when the model is still loading after that many
        seconds, so a search can answer by words instead of hanging.
        """
        if graph.count_embeddings() == 0:
            return None
        if wait is not None and not warmed.wait(wait):
            return None
        try:
            return normalise(embedder().embed_query(text))
        except EmbeddingError as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "public": public, "uploads": uploads}

    @app.get("/library")
    def library_info() -> dict[str, Any]:
        """Whose library this is, whether uploads are taken, and the latest import."""
        space = _visitor.get()
        status = imports.status(current_home())
        return {
            "own": space is not None or not public,
            "uploads": uploads,
            "public": public,
            "expires_at": space.expires_at.isoformat() if space is not None else None,
            "keep_hours": spaces.keep.total_seconds() / 3600 if spaces is not None else None,
            "max_upload_mb": max_upload // 1_000_000,
            "import": status.as_dict() if status is not None else None,
        }

    staged: dict[tuple[Path, str], list[int]] = {}
    staging = threading.Lock()

    def _target(request: Request, response: Response) -> tuple[Path, Callable[[], GraphStore]]:
        """The library uploads go into: the server's, or the visitor's, made on first use."""
        if not uploads:
            raise HTTPException(403, "this server does not take uploads")
        if not public:
            return library, lambda: open_store(library)
        assert spaces is not None
        space = _visitor.get()
        if space is None:
            token, space = spaces.create()
            max_age = int(spaces.keep.total_seconds())
            response.headers.append("set-cookie", _library_cookie(request, token, max_age))
        return space.home, space.open_store

    async def _receive(request: Request, path: Path, room: int) -> int:
        """Write the request body to ``path``, refusing more than ``room`` bytes."""
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > room:
            raise HTTPException(413, f"uploads may be up to {max_upload // 1_000_000} MB in all")
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with path.open("wb") as handle:
                async for piece in request.stream():
                    size += len(piece)
                    if size > room:
                        raise HTTPException(
                            413, f"uploads may be up to {max_upload // 1_000_000} MB in all"
                        )
                    handle.write(piece)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return size

    def _batch_folder(target: Path, batch: str) -> Path:
        if not _BATCH.fullmatch(batch):
            raise HTTPException(422, "a batch is 32 hexadecimal digits")
        return target / "uploads" / batch

    @app.post("/library/files")
    async def upload_file(
        request: Request, response: Response, batch: Annotated[str, Query()]
    ) -> dict[str, Any]:
        """Add one file to a batch of uploads; ``POST /library/import`` imports the batch.

        Send the file as the request body and its path in ``X-Filename``, such as
        ``Export/conversations.json`` for a file chosen with its folder. ``batch``
        is any 32 hexadecimal digits the client picks for the whole upload. The
        files of a batch may be up to the upload limit in all.
        """
        _changes_allowed(request)
        target, _ = _target(request, response)
        folder = _batch_folder(target, batch)
        with staging:
            used = staged.setdefault((target, batch), [0, 0])
            if used[1] >= MAX_BATCH_FILES:
                raise HTTPException(413, f"a batch may hold up to {MAX_BATCH_FILES:,} files")
            used[1] += 1
        if not folder.exists():
            _forget_stale_batches(target / "uploads")
        name = safe_relative(unquote(request.headers.get("x-filename") or "upload"))
        size = await _receive(request, folder / name, max_upload - used[0])
        with staging:
            used[0] += size
        return {"batch": batch, "files": used[1], "bytes": used[0]}

    @app.post("/library/import", status_code=202)
    async def import_upload(
        request: Request, response: Response, batch: Annotated[str | None, Query()] = None
    ) -> dict[str, Any]:
        """Import a batch of files sent to ``POST /library/files``, or one file sent here.

        Without ``batch``, the request body is the file and ``X-Filename`` its name.
        Archives are unpacked, chat exports recognised, and other files read as
        notes, in the background. On a public server the upload goes into the
        visitor's own library, made on the first upload, and a cookie remembers it.
        ``GET /library`` shows how far the import got.
        """
        _changes_allowed(request)
        target, opener = _target(request, response)
        if imports.running(target):
            raise HTTPException(409, "an import is already running for this library")
        if batch is not None:
            folder = _batch_folder(target, batch)
            with staging:
                used = staged.pop((target, batch), [0, 0])
            if not folder.exists() or not any(folder.iterdir()):
                raise HTTPException(400, "the batch holds no files")
            name = f"{used[1]:,} files" if used[1] != 1 else _only_file(folder)
        else:
            name = safe_relative(unquote(request.headers.get("x-filename") or "upload"))
            folder = _batch_folder(target, uuid.uuid4().hex)
            _forget_stale_batches(target / "uploads")
            try:
                size = await _receive(request, folder / name, max_upload)
                if size == 0:
                    raise HTTPException(400, "the upload is empty")
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            name = PurePosixPath(name).name
        return imports.start(target, folder, name, opener).as_dict()

    @app.get("/library/export")
    def export_library(
        format: Annotated[str, Query(pattern="^(archive|markdown)$")] = "archive",
    ) -> FileResponse:
        """The library as a ChatLore archive, or as a zip of Markdown files."""
        home = current_home()
        if not Library(home).stats():
            raise HTTPException(404, "the library is empty")
        folder = Path(tempfile.mkdtemp(prefix="chatlore-export-"))
        try:
            if format == "archive":
                target = folder / "chatlore-library.zip"
                with store() as graph:
                    export_archive(home, target, store=graph)
            else:
                export_markdown(Library(home), folder / "markdown")
                target = Path(
                    shutil.make_archive(
                        str(folder / "chatlore-markdown"), "zip", folder / "markdown"
                    )
                )
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return FileResponse(
            target,
            media_type="application/zip",
            filename=target.name,
            background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True),
        )

    @app.delete("/library")
    def delete_library(request: Request, response: Response) -> dict[str, str]:
        """Delete the visitor's own library now, stopping any import into it first."""
        _changes_allowed(request)
        space = _visitor.get()
        if space is None:
            if not public:
                raise HTTPException(403, "the server's library cannot be deleted from here")
            raise HTTPException(404, "you have no library of your own on this server")
        imports.cancel(space.home, then=lambda: _forget(space))
        # Removed with the same attributes, or a partitioned cookie would stay.
        response.headers.append("set-cookie", _library_cookie(request, '""', 0))
        return {"status": "deleted"}

    @app.get("/stats")
    def stats() -> dict[str, int]:
        with store() as graph:
            counts = graph.count_by_label()
            return {
                "conversations": counts[Label.CONVERSATION],
                "messages": counts[Label.MESSAGE],
                "chunks": counts[Label.CHUNK],
                "embeddings": graph.count_embeddings(),
                "entities": counts[Label.ENTITY],
                "topics": counts[Label.TOPIC],
                "facts": counts[Label.FACT],
            }

    @app.get("/search")
    def search(
        response: Response,
        q: Annotated[str, Query(min_length=1)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        source: Annotated[list[str] | None, Query()] = None,
        mode: Annotated[str, Query(pattern="^(words|hybrid)$")] = "hybrid",
    ) -> list[dict[str, Any]]:
        """Messages matching ``q``: by words only, or by words, meaning, and entities.

        While the embedding model is still loading, such as during its first
        download, a hybrid search answers by words and says so in the header
        ``X-ChatLore-Meaning: loading``.
        """
        with store() as graph:
            vector = embed(q, graph, wait=SEARCH_WAIT) if mode == "hybrid" else None
            if vector is None and mode == "hybrid" and not warmed.is_set():
                response.headers["X-ChatLore-Meaning"] = "loading"
            if vector is None:
                return [
                    {
                        "message_id": hit.node_id,
                        "conversation_id": hit.conversation_id,
                        "title": hit.title,
                        "source": hit.source,
                        "snippet": hit.snippet,
                        "matched": ["words"],
                    }
                    for hit in graph.search_text(
                        q, limit=limit, sources=source, labels=[Label.MESSAGE]
                    )
                ]
            return [
                {
                    "message_id": hit.message_id,
                    "conversation_id": hit.conversation_id,
                    "title": hit.title,
                    "source": hit.source,
                    "snippet": hit.snippet,
                    "matched": [
                        name
                        for name, found in (
                            ("words", hit.by_words),
                            ("meaning", hit.by_meaning),
                            ("entities", hit.by_entity),
                        )
                        if found
                    ],
                }
                for hit in hybrid_search(graph, q, vector, limit=limit, sources=source)
            ]

    @app.get("/conversations")
    def conversations(
        source: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        with store() as graph:
            return [
                {
                    "id": item.id,
                    "source": item.source,
                    "title": item.title,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "messages": item.message_count,
                }
                for item in graph.list_conversations(source=source, limit=limit, offset=offset)
            ]

    @app.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str) -> dict[str, Any]:
        """A conversation's messages, and the entities it mentions with the messages they are in."""
        with store() as graph:
            found = graph.get_conversation(conversation_id)
            mentioned = conversation_entities(graph, conversation_id) if found else []
        if found is None:
            raise HTTPException(404, "no such conversation")
        return {
            "id": found.id,
            "source": found.source.value,
            "title": found.title,
            "created_at": found.created_at.isoformat() if found.created_at else None,
            "entities": mentioned,
            "messages": [
                {
                    "id": message.id,
                    "parent_id": message.parent_id,
                    "role": message.role.value,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                    "text": message.text,
                }
                for message in found.messages
            ],
        }

    @app.get("/entities")
    def entities(
        q: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[dict[str, Any]]:
        """Entities matching ``q`` by name or summary, or the most mentioned ones."""
        with store() as graph:
            if q:
                hits = graph.search_text(q, limit=limit, labels=[Label.ENTITY])
                found = graph.get_nodes(hit.node_id for hit in hits)
                nodes = [found[hit.node_id] for hit in hits if hit.node_id in found]
            else:
                nodes = sorted(
                    graph.find_nodes(Label.ENTITY), key=lambda node: -int(node.props["mentions"])
                )[:limit]
            return [_entity(node) for node in nodes]

    @app.get("/entities/{entity_id}")
    def entity(entity_id: str) -> dict[str, Any]:
        with store() as graph:
            node = graph.get_node(entity_id)
            if node is None or node.label != Label.ENTITY:
                raise HTTPException(404, "no such entity")
            related = sorted(
                graph.neighbors(entity_id, [EdgeType.RELATED_TO], "both", limit=1_000_000),
                key=lambda pair: -int(pair[0].props.get("weight", 0)),
            )
            chunks = graph.neighbors(entity_id, [EdgeType.MENTIONS], "in", limit=1_000_000)
            titles = {
                str(chunk.props.get("conversation_id")): chunk.props.get("title")
                for _, chunk in chunks
            }
            topics = graph.neighbors(entity_id, [EdgeType.IN_TOPIC])
            known = facts_about(graph, [entity_id])[entity_id]
            return {
                **_entity(node),
                "descriptions": node.props.get("descriptions", []),
                "also_called": [
                    str(other.props["name"])
                    for _, other in graph.neighbors(entity_id, [EdgeType.SAME_AS], "both")
                ],
                "topic": _topic(topics[0][1], entities=10) if topics else None,
                "related": [
                    {
                        **_entity(other),
                        "relationship": (edge.props.get("descriptions") or [None])[0],
                        "weight": edge.props.get("weight"),
                    }
                    for edge, other in related[:20]
                ],
                "facts": [item.as_dict() for item in known[:50]],
                "conversations": [
                    {"id": conversation_id, "title": title}
                    for conversation_id, title in titles.items()
                ],
            }

    @app.get("/facts")
    def facts(
        q: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[dict[str, Any]]:
        """Facts whose statement or subject matches ``q``, or the newest facts."""
        with store() as graph:
            return [item.as_dict() for item in find_facts(graph, q, limit)]

    @app.get("/explore")
    def explore_view(
        entity: str | None = None,
        conversation: str | None = None,
        topic: str | None = None,
        source: Annotated[list[str] | None, Query()] = None,
        since: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}(-\d{2})?$")] = None,
        until: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}(-\d{2})?$")] = None,
        entities: Annotated[int, Query(ge=1, le=200)] = 40,
        conversations: Annotated[int, Query(ge=0, le=100)] = 16,
    ) -> dict[str, Any]:
        """Entities and the conversations they came up in, to explore the library as a graph.

        Around an ``entity``, a ``conversation``, or a ``topic``, or an overview;
        ``source``, ``since``, and ``until`` keep only some conversations.
        """
        window = Window(frozenset(source or ()), since, until)
        with store() as graph:
            try:
                return explore(
                    graph,
                    entity=entity,
                    conversation=conversation,
                    topic=topic,
                    window=window,
                    entities=entities,
                    conversations=conversations,
                )
            except NotFoundError as error:
                raise HTTPException(404, str(error)) from error

    @app.get("/explore/timeline")
    def explore_timeline(
        source: Annotated[list[str] | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        """How many conversations started in each month, oldest first."""
        with store() as graph:
            return timeline(graph, source or ())

    @app.get("/topics")
    def topics(
        q: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[dict[str, Any]]:
        """Topics whose report matches ``q``, or all topics, largest first."""
        with store() as graph:
            if q:
                hits = graph.search_text(q, limit=limit, labels=[Label.TOPIC])
                found = graph.get_nodes(hit.node_id for hit in hits)
                nodes = [found[hit.node_id] for hit in hits if hit.node_id in found]
            else:
                nodes = sorted(
                    graph.find_nodes(Label.TOPIC), key=lambda node: -int(node.props["size"])
                )[:limit]
            return [_topic(node, entities=10) for node in nodes]

    @app.get("/topics/{topic_id}")
    def topic(topic_id: str) -> dict[str, Any]:
        with store() as graph:
            node = graph.get_node(topic_id)
            if node is None or node.label != Label.TOPIC:
                raise HTTPException(404, "no such topic")
            members = graph.neighbors(topic_id, [EdgeType.IN_TOPIC], "in", limit=1_000_000)
            return {
                **_topic(node),
                "members": [
                    _entity(member)
                    for _, member in sorted(
                        members, key=lambda pair: -int(pair[1].props["mentions"])
                    )
                ],
            }

    @app.get("/graph")
    def graph(
        entity: str | None = None,
        topic: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 150,
    ) -> dict[str, Any]:
        """A part of the knowledge graph to draw: entities, their links, and their topics.

        With ``entity``, that entity and its most strongly related neighbours; with
        ``topic``, the topic's entities; otherwise the most mentioned entities that
        have relationships. Edges are the relationships among the chosen entities.
        """
        with store() as graph:
            if entity:
                center = graph.get_node(entity)
                if center is None or center.label != Label.ENTITY:
                    raise HTTPException(404, "no such entity")
                around = sorted(
                    graph.neighbors(entity, [EdgeType.RELATED_TO], "both", limit=1_000_000),
                    key=lambda pair: -int(pair[0].props.get("weight", 0)),
                )
                chosen = {center.id: center} | {
                    other.id: other for _, other in around[: max(0, limit - 1)]
                }
            elif topic:
                found = graph.get_node(topic)
                if found is None or found.label != Label.TOPIC:
                    raise HTTPException(404, "no such topic")
                members = graph.neighbors(topic, [EdgeType.IN_TOPIC], "in", limit=1_000_000)
                chosen = {
                    node.id: node
                    for _, node in sorted(members, key=lambda pair: -int(pair[1].props["mentions"]))
                }
                chosen = dict(list(chosen.items())[:limit])
            links: dict[str, list[Edge]] | None = None
            if not entity and not topic:
                every = graph.find_nodes(Label.ENTITY)
                related = graph.edges_many(
                    (node.id for node in every), [EdgeType.RELATED_TO], "both"
                )
                linked = [node for node in every if related[node.id]]
                linked.sort(key=lambda node: -int(node.props["mentions"]))
                chosen = {node.id: node for node in linked[:limit]}
                # The outgoing links are among the ones just fetched.
                links = {
                    node_id: [edge for edge in related[node_id] if edge.src == node_id]
                    for node_id in chosen
                }

            nodes, edges, topic_of = [], [], {}
            if links is None:
                links = graph.edges_many(chosen, [EdgeType.RELATED_TO], "out")
            for node_id, in_topic in graph.edges_many(chosen, [EdgeType.IN_TOPIC]).items():
                if in_topic:
                    topic_of[node_id] = in_topic[0].dst
            topic_nodes = graph.get_nodes(dict.fromkeys(topic_of.values()))
            topics = {
                topic_id: topic_nodes[topic_id].props.get("title")
                for topic_id in dict.fromkeys(topic_of.values())
                if topic_id in topic_nodes
            }
            for node in chosen.values():
                topic_id = topic_of.get(node.id)
                nodes.append({**_entity(node), "topic": topic_id if topic_id in topics else None})
                for edge in links[node.id]:
                    if edge.dst in chosen:
                        edges.append(
                            {
                                "source": node.id,
                                "target": edge.dst,
                                "weight": edge.props.get("weight", 1),
                                "description": (edge.props.get("descriptions") or [None])[0],
                            }
                        )
            return {
                "nodes": nodes,
                "edges": edges,
                "topics": [{"id": key, "title": title} for key, title in topics.items()],
            }

    @app.post("/chat", response_class=EventSourceResponse)
    def chat(request: ChatRequest, http: Request) -> Iterator[ServerSentEvent]:
        """Answer a question, streaming `sources`, then `token`s, then `done` or `error`.

        `sources` lists the numbered passages the answer may cite. Each `token`
        carries the next piece of text. `done` lists the numbers the answer
        cited; `error` says why it stopped.
        """
        # All database work happens before the first event, in one thread.
        try:
            with store() as graph:
                context: Context = retrieve(
                    graph, request.question, embed(request.question, graph), limit=request.sources
                )
        except HTTPException as error:
            yield ServerSentEvent(event="error", data={"message": str(error.detail)})
            return
        yield ServerSentEvent(event="sources", data=[_source(s) for s in context.sources])
        if context.empty:
            yield ServerSentEvent(event="done", data={"cited": [], "found": False})
            return
        if allowance is not None:
            refusal = allowance.take(http.client.host if http.client else "unknown")
            if refusal is not None:
                yield ServerSentEvent(event="error", data={"message": refusal})
                return
        try:
            llm = make_llm()
        except LLMError as error:
            yield ServerSentEvent(event="error", data={"message": str(error)})
            return
        written: list[str] = []
        try:
            for piece in answer(llm, context):
                written.append(piece)
                yield ServerSentEvent(event="token", data={"text": piece})
        except LLMError as error:
            yield ServerSentEvent(event="error", data={"message": str(error)})
            return
        finally:
            llm.close()
        numbers = [source.number for source in cited("".join(written), context)]
        yield ServerSentEvent(event="done", data={"cited": numbers, "found": True})

    # POST /mcp for assistants, next to the API.
    app.router.routes.extend(mcp_app.routes)

    # The web interface: plain files, no build step. Mounted last, so every API
    # route above takes precedence over a file of the same name.
    app.mount("/", _WebFiles(directory=WEB, html=True), name="web")
    return app
