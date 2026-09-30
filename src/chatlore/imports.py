"""Importing an uploaded export in the background: import, embed, and build the graph.

The web interface hands an upload to ``Imports``, which runs one import at a
time on a worker thread and keeps its status, so the page can show how far it
got while the server goes on answering other requests. The steps are the ones
the command line runs, in the same order: ``chatlore import``, ``chatlore
process``, and ``chatlore extract``. Everything each step finishes is kept, so
an import that fails or is cancelled halfway leaves what it had done.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from chatlore.archive import ArchiveError, import_archive
from chatlore.embeddings import Embedder, EmbeddingCache, EmbeddingError
from chatlore.extraction import ExtractionCache
from chatlore.importers import ImporterError
from chatlore.importers.intake import Intake
from chatlore.library import AddOutcome, Library
from chatlore.llm import LLM, LLMError
from chatlore.pipeline import (
    EmbeddingModelMismatchError,
    pending_duplicates,
    pending_extractions,
    pending_summaries,
    pending_topics,
    sync_chunks,
    sync_duplicates,
    sync_embeddings,
    sync_entities,
    sync_extractions,
    sync_summaries,
    sync_topics,
)
from chatlore.store import ConversationWriter, GraphStore

logger = logging.getLogger(__name__)


class _ClosableLLM(LLM, Protocol):
    def close(self) -> None: ...


class ImportCancelledError(Exception):
    """The import was stopped on request."""


@dataclass(slots=True)
class ImportStatus:
    """Where an import is: from ``waiting`` to ``done``, ``failed``, or ``cancelled``.

    The stages in between are ``importing``, ``embedding``, ``reading`` (the
    model reads the chunks), ``summarising``, ``linking`` (possible duplicates),
    and ``topics``, each with ``done`` out of ``total``.
    """

    file: str
    stage: str = "waiting"
    done: int = 0
    total: int = 0
    conversations: int = 0
    messages: int = 0
    skipped: int = 0
    """Records the importers could not read, inside files they did read."""
    sources: dict[str, int] = field(default_factory=dict)
    """Conversations found, by where they came from: chatgpt, document, email, ..."""
    skipped_files: int = 0
    skipped_shown: list[list[str]] = field(default_factory=list)
    """The first skipped files, each with the reason."""
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "running": self.running}


class Imports:
    """Runs uploads through the pipeline, one at a time, and keeps each library's latest."""

    def __init__(
        self,
        embedder: Callable[[], Embedder],
        llm: Callable[[], _ClosableLLM],
        extract_limit: int | None = None,
    ) -> None:
        self._embedder = embedder
        self._llm = llm
        self.extract_limit = extract_limit
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chatlore-import")
        self._lock = threading.Lock()
        self._status: dict[Path, ImportStatus] = {}
        self._cancelled: set[Path] = set()
        self._finished: dict[Path, list[Callable[[], None]]] = {}

    def status(self, home: Path) -> ImportStatus | None:
        with self._lock:
            return self._status.get(home)

    def running(self, home: Path) -> bool:
        status = self.status(home)
        return status is not None and status.running

    def start(
        self, home: Path, upload: Path, name: str, store: Callable[[], GraphStore]
    ) -> ImportStatus:
        """Queue the files in the folder ``upload`` for the library at ``home``.

        ``store`` opens the library's graph. The folder is deleted when the import ends.
        """
        status = ImportStatus(file=name)
        with self._lock:
            self._status[home] = status
            self._cancelled.discard(home)
        self._executor.submit(self._run, home, upload, store, status)
        return status

    def cancel(self, home: Path, then: Callable[[], None] | None = None) -> bool:
        """Stop the library's import at its next step. Returns whether one was running.

        ``then`` runs once the import has stopped, or at once when none runs.
        """
        with self._lock:
            status = self._status.get(home)
            running = status is not None and status.running
            if running:
                self._cancelled.add(home)
                if then is not None:
                    self._finished.setdefault(home, []).append(then)
        if not running and then is not None:
            then()
        return running

    def forget(self, home: Path) -> None:
        with self._lock:
            self._status.pop(home, None)

    def shutdown(self) -> None:
        with self._lock:
            self._cancelled.update(home for home, status in self._status.items() if status.running)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run(
        self, home: Path, upload: Path, store: Callable[[], GraphStore], status: ImportStatus
    ) -> None:
        def check() -> None:
            with self._lock:
                if home in self._cancelled:
                    raise ImportCancelledError

        def advance(count: int) -> None:
            status.done += count
            check()

        graph: GraphStore | None = None
        try:
            check()
            graph = store()
            status.stage = "importing"
            _import(upload, home, graph, status)
            check()
            self._embed(home, graph, status, advance)
            check()
            self._build_graph(home, graph, status, advance)
            status.stage = "done"
        except ImportCancelledError:
            status.stage = "cancelled"
        except (
            ImporterError,
            ArchiveError,
            EmbeddingError,
            EmbeddingModelMismatchError,
            LLMError,
        ) as error:
            status.stage, status.error = "failed", str(error)
        except Exception as error:  # a bad upload must not stop the worker
            logger.exception("import of %s failed", status.file)
            status.stage, status.error = "failed", f"the import stopped unexpectedly: {error}"
        finally:
            if graph is not None:
                graph.close()
            shutil.rmtree(upload, ignore_errors=True)
            status.finished_at = datetime.now(UTC).isoformat()
            with self._lock:
                self._cancelled.discard(home)
                waiting = self._finished.pop(home, [])
            for then in waiting:
                then()

    def _embed(
        self, home: Path, graph: GraphStore, status: ImportStatus, advance: Callable[[int], None]
    ) -> None:
        chunks = sync_chunks(graph, Library(home))
        status.stage, status.done = "embedding", 0
        status.total = chunks.total - graph.count_embeddings()
        with EmbeddingCache(home / "cache" / "embeddings.db") as cache:
            sync_embeddings(graph, self._embedder(), cache, on_progress=advance)

    def _build_graph(
        self, home: Path, graph: GraphStore, status: ImportStatus, advance: Callable[[int], None]
    ) -> None:
        """As ``chatlore extract``: entities, summaries, duplicates, then topics."""
        try:
            llm = self._llm()
        except LLMError as error:
            status.notes.append(f"The knowledge graph was not built: {error}")
            return
        try:
            with ExtractionCache(home / "cache" / "extractions.db") as cache:
                pending = len(pending_extractions(graph, cache, llm.name))
                limit = self.extract_limit
                status.stage, status.done = "reading", 0
                status.total = pending if limit is None else min(pending, limit)
                if limit is not None and pending > limit:
                    status.notes.append(
                        f"The model read {limit} of {pending} passages; the rest are "
                        "searchable but not in the knowledge graph."
                    )
                try:
                    sync_extractions(graph, llm, cache, limit=limit, on_progress=advance)
                except LLMError as error:
                    # As on the command line: keep what was read so far.
                    sync_entities(graph, cache, llm.name)
                    status.notes.append(f"The model stopped answering: {error}")
                    return
                sync_entities(graph, cache, llm.name)
                try:
                    status.stage, status.done = "summarising", 0
                    status.total = len(pending_summaries(graph))
                    sync_summaries(graph, llm, cache, on_progress=advance)
                    status.stage, status.done = "linking", 0
                    status.total = pending_duplicates(graph, cache, llm.name)
                    sync_duplicates(graph, llm, cache, on_progress=advance)
                    status.stage, status.done = "topics", 0
                    status.total = pending_topics(graph, cache, llm.name)
                    sync_topics(graph, llm, cache, on_progress=advance)
                except LLMError as error:
                    status.notes.append(
                        f"The model stopped answering while {status.stage}: {error}"
                    )
        finally:
            llm.close()


_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')
_MAX_PART = 120
_MAX_PARTS = 24
_SKIPPED_SHOWN = 50


def safe_relative(name: str) -> str:
    """An upload's path within its batch, made safe: no absolute paths, no ``..``.

    Browsers send a file's path within the folder it was chosen from, such as
    ``Export/conversations.json``; that path is kept, since it tells the
    importers what they are reading.
    """
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):
        part = _UNSAFE.sub("_", part).strip().rstrip(". ")
        if not part or part in {".", ".."}:
            continue
        parts.append(part[:_MAX_PART])
    return "/".join(parts[-_MAX_PARTS:]) or "upload"


def _import(folder: Path, home: Path, graph: GraphStore, status: ImportStatus) -> None:
    """Add everything the uploaded files hold to the library and its graph."""
    with tempfile.TemporaryDirectory(prefix="chatlore-unpacked-") as workdir:
        intake = Intake(Path(workdir))
        issues: list[object] = []
        try:
            intake.scan([folder])
            with (
                Library(home) as library,
                graph.transaction(),
                ConversationWriter(graph) as writer,
            ):
                for conversation in intake.conversations(issues.append):
                    status.messages += len(conversation.messages)
                    if library.add(conversation) is not AddOutcome.UNCHANGED:
                        status.conversations += 1
                        writer.add(conversation)
            for archive, _ in intake.archives:
                report = import_archive(archive, home, store=graph)
                status.conversations += (
                    report.outcomes[AddOutcome.NEW] + report.outcomes[AddOutcome.UPDATED]
                )
                status.messages += report.messages
                intake.sources["archive"] += sum(report.outcomes.values())
        finally:
            status.sources = {kind: count for kind, count in intake.sources.items() if count}
            status.skipped = len(issues)
            status.skipped_files = len(intake.skipped)
            status.skipped_shown = [
                [item.path, item.reason] for item in intake.skipped[:_SKIPPED_SHOWN]
            ]
        if not intake.found_anything():
            raise ImporterError("nothing ChatLore can read was found in the upload")
