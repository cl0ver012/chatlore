# ChatLore - Development Plan

Status: DRAFT v0.1 (2026-09-16), for review before any code is written.
Working name `chatlore` is a placeholder; everything below renames cleanly.

## 1. Vision

ChatLore is a local-first, open-source graph knowledge base built from your own
conversations and documents. It imports chat history from ChatGPT, Claude, and
Gemini (later: local coding-agent sessions, notes, PDFs), links everything into
one graph of conversations, entities, topics, and facts with full provenance,
and gives you one central chat that answers from all of it with citations. It
plugs into the tools you already use through an MCP server, a REST API, and a
CLI.

One line: **All your AI conversations, one graph, one chat.**

Target users: people who use several AI assistants and lose track of what they
discussed where; developers who want their AI memory to belong to them and stay
usable no matter which model they switch to.

Non-goals for v1: multi-user / teams, cloud sync, mobile apps, real-time capture
from the assistant web UIs (a browser extension is a stretch goal), replacing
Obsidian or Notion.

## 2. v1 scope

Must have:

1. Import ChatGPT export (`conversations.json`), Claude.ai export
   (`conversations.json`), Gemini via Google Takeout.
2. Import Markdown folders (Obsidian vaults) and plain text; manual notes.
3. Canonical conversation model; raw exports kept content-addressed so any
   stage can be re-run later.
4. Graph store with nodes, edges, full-text and vector indexes. Embedded SQLite
   backend (zero setup) and a FalkorDB backend (Docker) behind one interface.
5. Hybrid search (BM25 + vector + graph expansion) with provenance.
6. LLM-based extraction of entities, topics, and facts, each linked back to the
   source message; pluggable providers (OpenAI-compatible incl. Ollama / vLLM /
   Qwen, Anthropic, Gemini).
7. Central chat: web UI, streaming answers, citations, model switcher; chat
   sessions are ingested back into the graph.
8. Web UI: sources / import, conversation browser, graph explorer, entity
   pages, chat, settings.
9. MCP server (stdio + streamable HTTP) so Claude Desktop, Claude Code, Cursor,
   ChatGPT, and Gemini CLI can query and write to the knowledge base.
10. REST API with OpenAPI docs; CLI.
11. Hosted read-only demo with synthetic data; docs; README with a GIF.

Nice to have (v1.x): Discord bot; Claude Code / Codex JSONL session import;
PDF and URL import; browser extension for live capture; export to Obsidian /
GraphML; scheduled re-import.

## 3. Key decisions (please confirm or change)

| # | Decision | Proposal | Alternatives | Why |
|---|---|---|---|---|
| 1 | Name | `chatlore` for repo, CLI, PyPI, npm | recollect, lore, mnemo (taken on PyPI and npm) | short, descriptive, available on PyPI, npm, and GitHub |
| 2 | Backend language | Python 3.12 managed by uv | TypeScript end to end | ML ecosystem and your audience; 3.12 pinned because the machine has 3.14 and ML wheels lag behind |
| 3 | Graph storage | One `GraphStore` interface, two backends: SQLite (default, embedded, FTS5 + sqlite-vec) and FalkorDB (Docker, Cypher, vector + full-text indexes) | Neo4j; Kuzu (archived Oct 2025); networkx only | zero-setup install for users; FalkorDB is the stack your contact uses and scales further; no Docker on this machine today, so SQLite first |
| 4 | Embeddings | fastembed (ONNX, local, default `BAAI/bge-small-en-v1.5`); optional OpenAI, Gemini, Ollama `Qwen3-Embedding` | sentence-transformers (needs torch) | small, fast, no torch, works offline |
| 5 | LLM providers | Own thin adapter: OpenAI-compatible (OpenAI, Ollama, vLLM, DeepSeek, DashScope / Qwen, OpenRouter), Anthropic, Gemini | LiteLLM, LangChain | readable code for a portfolio, few dependencies |
| 6 | API | FastAPI + SSE streaming, pydantic v2 | Django, Flask | async, OpenAPI for free |
| 7 | Frontend | React 19 + Vite + TypeScript + Tailwind + shadcn/ui; graph view with sigma.js / graphology | Next.js, SvelteKit | static SPA deploys to Cloudflare Pages, no SSR needed |
| 8 | Repo layout | Monorepo: `src/chatlore/` (Python package), `web/`, `docs/`, `deploy/` | separate repos | one PR can cover API + UI; simpler for reviewers |
| 9 | License | MIT | Apache-2.0 | simplest for adoption |
| 10 | Hosting | Web on Cloudflare Pages; API + FalkorDB on Fly.io (or a small VPS) in demo mode with synthetic data | all on Cloudflare (Python Workers too limited) | matches the advice you were given; demo must never contain real chats |
| 11 | Process | `main` protected; feature branches; one PR per step; Conventional Commits; squash merge; CI on every PR; no AI attribution trailers | direct commits to main | you asked for reviewable steps |

## 4. Architecture

```
            +-------------------+     +-------------------+     +-------------------+
 Sources    | ChatGPT export    |     | Claude export     |     | Gemini Takeout    |   ... Markdown, notes, agent sessions
            +---------+---------+     +---------+---------+     +---------+---------+
                      |                         |                         |
                      v                         v                         v
            +---------------------------------------------------------------------+
            | Importers  (chatlore/importers/*)  ->  Canonical Conversation JSON   |
            +----------------------------------+----------------------------------+
                                               |
                                               v
            +---------------------------------------------------------------------+
            | Pipeline   ingest -> chunk -> embed -> extract (entities/topics/facts)|
            |            every step idempotent, versioned, provenance-preserving    |
            +----------------------------------+----------------------------------+
                                               |
                                               v
            +---------------------------------------------------------------------+
            | GraphStore interface                                                |
            |   SQLite backend (default)       |   FalkorDB backend (Docker)       |
            |   nodes/edges tables, FTS5,      |   Cypher, vector + fulltext idx   |
            |   sqlite-vec                     |                                   |
            +----------------------------------+----------------------------------+
                                               |
                       +-----------------------+-----------------------+
                       v                       v                       v
            +----------------+      +--------------------+     +----------------+
            | Retrieval      |      | FastAPI REST + SSE |     | MCP server     |
            | hybrid search  |----->| /search /chat ...  |     | search/ask/... |
            +----------------+      +---------+----------+     +-------+--------+
                                              |                        |
                                              v                        v
                                    +------------------+     Claude Desktop, Claude Code,
                                    | Web UI (React)   |     Cursor, ChatGPT, Gemini CLI
                                    | chat, graph, ... |
                                    +------------------+
```

Principles:

- Local-first: your data stays in a folder you own.
- Provenance everywhere: every extracted fact links to the message it came from.
- Deterministic before semantic: import and search work with no LLM at all;
  extraction is an optional enrichment that can be re-run with a better model.
- Model-agnostic: any provider for chat, extraction, and embeddings, swappable
  at any time without re-importing.

## 5. Data model

### 5.1 Canonical Conversation (importer output)

```json
{
  "id": "conv_<sha256 of source + external_id>",
  "source": "chatgpt | claude | gemini | markdown | chatlore | claude_code | codex",
  "external_id": "...",
  "title": "...",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "...",
  "model": "gpt-5 | claude-opus-5 | ...",
  "messages": [
    {
      "id": "msg_<hash>", "parent_id": null,
      "role": "user | assistant | system | tool",
      "created_at": "...",
      "content": [{"type": "text", "text": "..."}, {"type": "code", "lang": "py", "text": "..."}],
      "attachments": [{"name": "...", "mime": "...", "hash": "..."}],
      "metadata": {}
    }
  ],
  "metadata": {"raw_hash": "sha256 of the original record", "importer_version": "1"}
}
```

Branching (ChatGPT regenerate / edit trees) is preserved through `parent_id`;
the default reading view follows the "current" branch.

### 5.2 Graph schema

Nodes (label: key properties):

- `Source`: id, kind, path, imported_at, stats
- `Conversation`: id, source, title, created_at, updated_at, model, message_count, hash
- `Message`: id, role, created_at, text, hash, position
- `Chunk`: id, text, embedding, token_count, order
- `Entity`: id, name, type (person | org | project | tool | tech | concept | place | other), aliases, summary, embedding
- `Topic`: id, name, summary
- `Fact`: id, statement, confidence, valid_from, valid_to, extractor, model, hash
- `Note`: id, text, created_at (manual notes)
- `Document`: id, title, path, mime, hash (markdown / pdf files)

Edges:

- `(Source)-[:CONTAINS]->(Conversation | Document)`
- `(Conversation)-[:HAS_MESSAGE]->(Message)`, `(Message)-[:REPLIES_TO]->(Message)`
- `(Message | Document | Note)-[:HAS_CHUNK]->(Chunk)`
- `(Chunk)-[:MENTIONS {offsets}]->(Entity)`
- `(Conversation | Document)-[:ABOUT {weight}]->(Topic)`
- `(Fact)-[:ASSERTED_IN]->(Chunk)` provenance, always present
- `(Fact)-[:SUBJECT]->(Entity)`, `(Fact)-[:OBJECT]->(Entity)`
- `(Fact)-[:SUPERSEDES]->(Fact)` temporal, driven by valid_from / valid_to
- `(Entity)-[:RELATED_TO {weight, kind}]->(Entity)`
- `(Entity)-[:SAME_AS]->(Entity)` entity resolution, explicit so it can be undone

Every node carries `hash` (sha256 of canonical content) and `created_at`.
Every derived node carries `extractor` and `extractor_version`, so re-running
extraction with a better model is a clean re-derivation, never a mutation of
source data.

### 5.3 Storage layout on disk

```
~/.chatlore/                (or $CHATLORE_HOME)
  config.toml               providers, models, paths
  raw/<sha256>.json         original export records, content-addressed
  chatlore.db               SQLite backend (nodes, edges, fts, vectors)
  cache/                    embedding cache, model cache
```

## 6. Importers

| Source | Input | Notes and known quirks |
|---|---|---|
| ChatGPT | `conversations.json` from Settings > Data controls > Export (zip) | tree in `mapping` keyed by node id; follow `current_node` parents for the main branch; `content.content_type` in {text, code, multimodal_text, execution_output, ...}; epoch timestamps; skip hidden system nodes |
| Claude.ai | `conversations.json` from Settings > Privacy > Export data | list of {uuid, name, created_at, updated_at, chat_messages[{uuid, sender: human / assistant, text, content[], created_at, attachments, files}]} |
| Gemini | Google Takeout > My Activity > Gemini Apps (`MyActivity.json` or `.html`) | messiest source: entries are per prompt with `title` "Prompted ..." and `time`; responses are sometimes absent or only in the HTML variant; build a best-effort parser, validate against a real export, document the limitation honestly |
| Markdown / Obsidian | folder path | one `Document` per file; wikilinks become `RELATED_TO` edges; frontmatter kept as metadata |
| Notes | CLI / UI / MCP | `Note` node, chunked and embedded like everything else |
| ChatLore chat | internal | every central-chat session is stored as a `Conversation` with `source=chatlore`, so the KB learns from its own use |
| Claude Code / Codex (v1.x) | `~/.claude/projects/**/*.jsonl`, `~/.codex/sessions` | follow-up from the earlier plan |

Every importer is a pure function `parse(path) -> Iterator[CanonicalConversation]`,
unit-tested against anonymised fixture files, idempotent on re-import
(hash-based dedupe), and never mutates its input.

## 7. Pipeline

1. **ingest**: store the raw record, upsert `Conversation` / `Message` nodes, dedupe by hash.
2. **chunk**: split messages at roughly 400 tokens with overlap; keep code blocks whole where possible.
3. **embed**: batch through the configured embedder; cached by (model, text hash).
4. **extract** (optional, needs an LLM): per conversation, one structured-output
   call returning entities, topics, and facts with source chunk ids; entity
   resolution by normalised name plus embedding similarity above a threshold,
   recorded as `SAME_AS`; facts get `valid_from` = message time; a later
   contradicting fact adds `SUPERSEDES`.
5. **index**: FTS5 over `Message` / `Chunk` / `Note` / `Document` text; vector index over `Chunk` and `Entity` embeddings.

Run as `chatlore import <source> <path>` then `chatlore process` (or
`--process` in one go). Every stage records its version per node, so upgrades
reprocess only what changed.

## 8. Retrieval and chat

- `search(query)`: BM25 hits + vector kNN hits fused with reciprocal rank
  fusion, then one-hop graph expansion (same-conversation neighbours, entity
  neighbours), optional rerank; returns chunks with conversation / message provenance.
- `ask(question)`: retrieval, context assembly under a token budget with
  citation markers, streamed LLM answer, citations resolved to conversation
  links. Question and answer are stored as a new `Conversation`.
- `entity_graph(name, depth)`: subgraph for the explorer.
- `timeline(entity | topic)`: facts ordered by `valid_from` with supersession.

## 9. Interfaces

REST (FastAPI, `/api/v1`):

- `POST /sources/import` (multipart or path), `GET /sources`, `DELETE /sources/{id}`
- `GET /conversations`, `GET /conversations/{id}`
- `GET /search?q=&k=&sources=`
- `POST /chat` (SSE stream), `GET /chats`, `GET /chats/{id}`
- `GET /graph/entities/{id}`, `GET /graph/neighbourhood?id=&depth=`, `GET /graph/topics`
- `POST /notes`
- `GET /settings/providers`, `PUT /settings/providers`
- `GET /health`, `GET /stats`

MCP server (`chatlore mcp`, stdio and streamable HTTP): tools `search`, `ask`,
`get_conversation`, `list_sources`, `add_note`, `entity_graph`, `timeline`;
resources for recent conversations. Install docs for Claude Desktop, Claude
Code, Cursor, ChatGPT connectors, Gemini CLI.

CLI (`chatlore`): `init`, `import`, `process`, `search`, `ask`, `serve` (API +
web), `mcp`, `export` (json | graphml | obsidian), `stats`, `doctor`.

Web UI screens: Sources (import wizard, progress), Conversations (list, filters
by source / date, reader with branch switch), Graph (sigma.js explorer, click to
expand, filters), Entity page (summary, facts timeline, mentions), Chat
(streaming, citations open the source message, model switcher), Settings
(providers; keys stored locally only).

## 10. Milestones and PR plan

Each PR should be reviewable in under 20 minutes. "Est." is focused dev days
with agent assistance.

| M | PRs | Deliverable | Acceptance | Est. |
|---|---|---|---|---|
| M0 Bootstrap | 1 repo skeleton + README + LICENSE + CI; 2 dev tooling (uv, ruff, mypy, pytest, pre-commit); 3 `docs/` with this plan | `uv run chatlore --help` works, CI green | 1 d |
| M1 Core model + SQLite store | 4 canonical models (pydantic); 5 `GraphStore` interface + SQLite backend with migrations; 6 FTS5 + sqlite-vec indexes | store round-trips conversations, search returns hits, tests pass | 2 d |
| M2 Importers | 7 ChatGPT; 8 Claude; 9 Gemini; 10 Markdown + notes; 11 `import` CLI + dedupe | each importer parses fixture files; re-import is a no-op | 3 d |
| M3 Pipeline | 12 chunking + embedder interface + fastembed; 13 hybrid search + RRF + graph expansion | `chatlore search` returns cited chunks from your real exports | 2 d |
| M4 Extraction | 14 LLM provider adapter (OpenAI-compatible, Anthropic, Gemini) with structured output; 15 entity / topic / fact extraction + provenance; 16 entity resolution + temporal facts | entities appear with mentions; facts link to chunks; tests use recorded LLM responses | 3 d |
| M5 API | 17 FastAPI app: sources, conversations, search; 18 chat SSE + chat storage; 19 graph endpoints + OpenAPI docs | `chatlore serve` works; API docs render | 2 d |
| M6 Web UI | 20 Vite scaffold + layout + API client; 21 Sources + Conversations; 22 Chat with citations; 23 Graph explorer + Entity page; 24 Settings | end to end: import an export, chat with citations in the browser | 4 d |
| M7 MCP | 25 MCP server + tools; 26 install docs per client | Claude Desktop can search and ask the KB | 1 d |
| M8 FalkorDB backend | 27 FalkorDB `GraphStore` + docker-compose; 28 backend parity tests in CI (service container) | the same test suite passes on both backends | 2 d |
| M9 Deploy + demo | 29 Dockerfile + compose prod; 30 synthetic demo dataset generator; 31 Cloudflare Pages + Fly.io deploy, read-only demo mode | public demo URL | 2 d |
| M10 Launch | 32 README polish + GIF + architecture doc; 33 blog post + social posts; 34 v0.1.0 release + CHANGELOG | tagged release | 1 d |

Total about 23 focused days; realistic calendar time 3 to 4 weeks part-time.
M3 (search over your real exports) and M6 (chat in the browser) are the two
demo-able checkpoints; M6 is worth showing to your contact before M8-M10 are
done.

## 11. Engineering conventions

- Branches: `main` protected (PR + CI required). Feature branches
  `feat/<milestone>-<topic>`, fixes `fix/<topic>`, docs `docs/<topic>`.
- Commits: Conventional Commits, e.g. `feat(importers): add ChatGPT export parser`.
  One logical change per commit. No AI attribution trailers or footers of any kind.
- PRs: template with Summary / Changes / How tested / Screenshots; squash
  merge; linked to a milestone issue; CI must pass.
- CI (GitHub Actions): ruff format + lint, mypy (strict on core packages),
  pytest with coverage on 3.12; web: eslint, tsc, vite build; FalkorDB job
  with a service container from M8.
- Tests: unit tests for every importer with anonymised fixtures; store
  contract tests run against every backend; API tests with httpx; recorded LLM
  responses so CI never needs network or keys.
- Versioning: SemVer, `CHANGELOG.md` (Keep a Changelog), GitHub Releases, PyPI
  publish from tag.
- Docs: `docs/` (this plan, architecture, importer notes, MCP setup), README
  with a 60-second quickstart, MkDocs site later.
- Privacy: fixtures and demo data are synthetic; `.gitignore` covers
  `~/.chatlore`, export files, `.env`; `chatlore doctor --privacy` warns if
  real export files sit inside the repo.

## 12. Setup on your machine (M0 prerequisites)

- Install uv (`winget install astral-sh.uv`), then `uv python install 3.12`.
- Install Node LTS (`winget install OpenJS.NodeJS.LTS`) and pnpm (`npm i -g pnpm`).
- Optional now, needed for M8: Docker Desktop with WSL2 (FalkorDB) and Ollama
  (local Qwen3 for extraction and chat).
- Set `git config --global user.name` and `user.email` (currently unset).
- Request your data exports today, they take hours to a day to arrive:
  ChatGPT (Settings > Data controls > Export data), Claude (Settings > Privacy >
  Export data), Gemini (takeout.google.com, "My Activity" > Gemini Apps, JSON).
- API keys in `.env` (never committed): at least one of `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or an Ollama / vLLM base URL.

## 13. Risks

- Gemini export format is inconsistent and may lack responses. Mitigation:
  best-effort parser, clear docs, later browser-extension capture.
- Extraction quality depends on the model. Mitigation: extraction is optional
  and re-runnable; evaluating Qwen3 against API models on your own data is a
  good blog post in itself.
- Python 3.14 on this machine. Mitigation: uv-managed 3.12.
- Scope: browser extension, bots, and multi-user are explicitly deferred.
- Privacy: real exports must never enter the repo or the demo.

## 14. Questions for you

1. Name: `chatlore` OK, or another? (recollect, lore, mnemo are taken on PyPI and npm.)
2. SQLite first, FalkorDB as the second backend in M8: OK?
3. Which LLM / embedding keys do you have for development: OpenAI, Anthropic, Gemini, DashScope (Qwen), Ollama?
4. Do you already have ChatGPT / Claude / Gemini exports? If not, request them now.
5. Public repo from M0, or private until M3?
6. React + Vite + Tailwind for the web UI: OK?
7. Milestone order OK? An alternative is to pull M7 (MCP) right after M3 so the
   KB is usable from Claude Desktop before the web UI exists.

Once you confirm, the first step is M0: create the repo under `cl0ver012`, push
the skeleton, and open PR #1.
