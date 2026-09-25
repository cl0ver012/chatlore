# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `chatlore serve --public` for hosting a demo behind a proxy: questions sent to the model are
  limited for each visitor per hour and for everyone per day (`--questions-per-hour`,
  `--questions-per-day`), visitors are told apart by the proxy's forwarded address, and the web
  interface says it is a demo. `/health` reports whether a server is public.
- `chatlore serve` also serves the MCP server at `/mcp` over streamable HTTP, for assistants
  that connect to a URL, such as ChatGPT. A private server only answers MCP requests addressed
  to this machine.
- A `Dockerfile` for the hosted demo, with the demo library and embedding model built in. CI
  builds it, starts it, and checks the web interface, the API, and a tool call over MCP. Guide
  in `docs/hosting.md`.

## [0.1.0] - 2026-09-24

### Added

- Repository skeleton with `src/` layout, `uv` project configuration, and MIT license.
- `chatlore` command-line entry point with `--version` and a `doctor` command.
- GitHub Actions CI running ruff, mypy, and pytest on Linux and Windows.
- pre-commit configuration with ruff and basic hygiene hooks.
- `uv.lock` with locked installs in CI, plus a manual Lock workflow to refresh it.
- Canonical conversation model (`chatlore.models`) that every importer targets, with
  branch-aware message trees, UTC-normalised timestamps, and strict validation.
- Deterministic conversation and message ids plus content hashes (`chatlore.ids`).
- Importers for ChatGPT, Claude, and Gemini (Google Takeout) exports and for Markdown
  folders such as Obsidian vaults. Zips, extracted folders, and single files are accepted,
  branches from regenerated or edited messages are preserved, and records that cannot be
  parsed are skipped and reported instead of aborting the import.
- `chatlore import` with source detection and `--dry-run`, `chatlore note`, and
  `chatlore stats`.
- An on-disk library of plain JSON files with idempotent, hash-based imports.
- Importer guide in `docs/importers.md`.
- Graph store interface (`chatlore.store.GraphStore`) with a SQLite backend: nodes and
  edges in tables, full-text search through FTS5, vector search through sqlite-vec, and
  conversations mapped to the graph and back. The same contract tests will run against
  the FalkorDB backend later.
- `chatlore search` for full-text search across every imported message, and
  `chatlore index --rebuild` to recreate the database from the library. `import` and
  `note` keep the database in step automatically.
- Chunking (`chatlore.chunking`): messages are split into retrieval-sized chunks of whole
  paragraphs and whole code blocks with a short prose overlap. Tool output and other machine
  text is deliberately not chunked.
- `chatlore process` keeps chunks in the graph in step with the library and only touches
  what changed, so embeddings on unchanged text survive a re-import.
- `GraphStore.find_nodes(label, where)` for property lookups.
- Local embeddings (`chatlore.embeddings`) through fastembed, with a disk cache keyed by
  model and text hash so a rebuilt database costs no embedding time. `chatlore process`
  embeds after chunking, resumes after an interruption, and refuses to mix two models;
  `--no-embed` and `--reembed` control it.
- `chatlore search --semantic` finds chunks by meaning and shows a similarity score.
- Store interface: `nodes_without_embedding`, `count_embeddings`, `clear_embeddings`,
  `get_meta`, `set_meta`.
- Hybrid search (`chatlore.search`): full-text hits on messages and vector hits on chunks are
  merged with reciprocal rank fusion, with chunks folded into their message so each message
  shows once. `chatlore search --hybrid` shows whether a hit matched by words, meaning, or both.
- Language model client (`chatlore.llm`) for any OpenAI-compatible chat API. OpenRouter with
  `deepseek/deepseek-v4-flash` and reasoning off is the default; Ollama, vLLM, and other local
  servers work by setting `CHATLORE_LLM_BASE_URL`. `CHATLORE_LLM_REASONING` sets how much the
  model may think before answering. `chatlore doctor` shows the configured model, reasoning, and
  whether a key is set. Model guide, with measurements of seven models, in `docs/models.md`.
- `chatlore extract` reads chunks with the language model and builds the entity graph: entities,
  relationships between them, and a link from every chunk to the entities it mentions. Answers
  are cached by model, prompt version, and text, so repeated and interrupted runs only read new
  text and a rebuilt database costs no model calls. Guide in `docs/extraction.md`.
- Entities mentioned more than once get a summary of at most 50 words, cached so it is written
  once. Names that differ only in case, spacing, hyphens, trailing punctuation, or a plural "s"
  are one entity, and invented types such as company or city count as the suggested ones.
- Possible duplicates, such as "AWS" and "Amazon Web Services", are proposed from their names and
  judged by the model; confirmed pairs are linked with `SAME_AS` and both entities are kept.
- Topics: Leiden community detection groups related entities, groups over 40 entities are split
  again, and the model writes a report on each topic with a title, summary, and findings.
- `chatlore topics` lists topics or finds them by words, and `chatlore entity` shows an entity's
  summary, other names, topic, relationships, and the conversations that mention it.
- `chatlore search --hybrid` also finds messages through the entities they mention.
- `chatlore ask` answers a question from the library, citing the passages it used. Passages come
  from meaning and from the entities the question names, merged with rank fusion; the model also
  gets the entities' summaries and their topics' reports. Guide in `docs/chat.md`.
- `chatlore serve` runs a REST API on 127.0.0.1: health, stats, search, conversations, entities,
  and topics, and `POST /chat`, which streams sources, answer tokens, and citations as
  server-sent events.
- The language model client can stream answers.
- A web interface at `/` when `chatlore serve` runs: ask with streamed, cited answers, search,
  conversations, topics, and an interactive knowledge graph drawn with a force-directed layout,
  coloured by topic, that can be explored around an entity or a topic. Plain files, no build
  step, nothing loaded from the internet. Guide in `docs/web.md`.
- `GET /graph` returns part of the knowledge graph for drawing.
- `chatlore mcp`, an MCP server over standard input and output, so Claude Desktop, Claude
  Code, Cursor, and other MCP clients can use the library: `ask_context` gathers the cited
  passages for a question, and `search`, `conversation`, `entity`, `topics`, and `topic`
  read the rest. Read-only, and no model key needed. Setup in `docs/mcp.md`.
- `chatlore demo` loads a made-up library of 32 conversations, with its knowledge graph already
  built, into `~/.chatlore-demo` and opens the web interface on it. No export and no API key
  needed, and the real library is not touched.
- `chatlore export` writes the whole library into one archive: conversations, knowledge graph,
  and the cached embeddings and model answers. `chatlore import` recognises an archive and
  restores it without model calls, into an empty library or next to other conversations.
  `chatlore export --markdown` writes one readable Markdown file per conversation. Guide in
  `docs/export.md`.
- `chatlore --home <folder>` picks the library for any command, like `CHATLORE_HOME`.
- CI builds the package, installs the wheel, and runs the demo from it. A Release workflow
  publishes a tagged version to PyPI through trusted publishing and creates a GitHub release;
  steps in `docs/releasing.md`.
- CLI output shows paths relative to the home directory.

### Changed

- An empty word search now says what is going on: nothing imported yet, no matches, or no
  matches with a pointer to `--semantic` when embeddings exist. It used to ask whether the
  library was imported, which read like an error.
- The embedding model is loaded from disk without contacting the Hugging Face hub once it
  has been downloaded. Semantic search starts faster, prints no download progress, and
  works offline.
- Re-importing a conversation updates the messages that are still present in place instead
  of deleting and re-inserting them, so their chunks and embeddings are kept.

### Fixed

- An empty answer from the language model, or one cut off at the token limit, no longer stops
  extraction; that batch is asked again on the next run. Found on a real export, where one empty
  answer ended a run after 600 of 2,043 chunks.
- Claude exports with conversations whose every message is blank no longer abort with
  `max() iterable argument is empty`; they are skipped and reported once. Found with a
  real export, where 234 of 355 conversations were blank.
- Claude `injected_prompt_block` content and tool blocks marked `hidden_in_chat` are no
  longer imported; failed tool results are labelled as errors; messages that only shared
  files are kept as `[files: ...]`.
- Importing is much faster: one transaction per import, `synchronous=NORMAL` on disk, an
  external-content FTS5 table keyed by row id instead of a full-index scan per delete, and
  a library index so unchanged conversations are detected without opening their files.
  A real 121-conversation export went from 39 s to under 5 s.
- Skipped records are grouped by reason in the import summary.
