# ChatLore

> All your AI conversations, one graph, one chat.

[![CI](https://github.com/cl0ver012/chatlore/actions/workflows/ci.yml/badge.svg)](https://github.com/cl0ver012/chatlore/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**Status: pre-alpha.** Importing and search work today: ChatGPT, Claude, Gemini,
and Markdown exports land in a local library and a SQLite graph store you can
search from the terminal by words, by meaning, or both. The knowledge graph and
chat are next. The roadmap below shows the order.

## Try it

```bash
git clone https://github.com/cl0ver012/chatlore.git
cd chatlore
uv sync
uv run chatlore import path/to/chatgpt-export.zip
uv run chatlore search "postgres index"
uv run chatlore process                                  # chunk and embed, local model
uv run chatlore search "why was my query slow" --semantic
uv run chatlore search "slow postgres query" --hybrid     # words and meaning together
uv run chatlore stats
```

The source is detected from the file. Importing is idempotent, so re-running it
after a fresh export only adds what changed. How to get each export, what is
kept, and the known limits are in [docs/importers.md](docs/importers.md).

## What ChatLore will do

ChatLore is a local-first, open-source graph knowledge base built from your own
conversations and documents.

- **Import** your history from ChatGPT, Claude, and Gemini exports, plus
  Markdown folders, notes, and later local coding-agent sessions.
- **Link** everything into one graph of conversations, entities, topics, and
  facts, with every extracted fact pointing back to the message it came from.
- **Search** across all of it with full-text, vector, and graph retrieval
  combined.
- **Chat** in one place with citations, using any model provider you like:
  OpenAI-compatible endpoints (including Ollama, vLLM, and Qwen), Anthropic,
  or Gemini.
- **Integrate** with the tools you already use through an MCP server, a REST
  API, and a CLI.

Your data stays in a folder you own. Import and search work without any LLM;
extraction is an optional enrichment you can re-run with a better model later.

## Roadmap

| Milestone | Deliverable | Status |
|---|---|---|
| M0 | Repository skeleton and CI | done |
| M1 | Core data model and embedded SQLite graph store | done |
| M2 | Importers: ChatGPT, Claude, Gemini, Markdown, notes | done |
| M3 | Chunking, embeddings, hybrid search | done |
| M4 | Entity, topic, and fact extraction with provenance | planned |
| M5 | REST API with streaming chat | planned |
| M6 | Web UI: conversations, graph explorer, chat | planned |
| M7 | MCP server for Claude Desktop, Claude Code, Cursor, ChatGPT | planned |
| M8 | FalkorDB backend | planned |
| M9 | Hosted demo | planned |
| M10 | v0.1.0 release | planned |

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/) and Git. uv installs the pinned
Python version for you.

```bash
git clone https://github.com/cl0ver012/chatlore.git
cd chatlore
uv sync
uv run chatlore --help
```

Checks that CI runs on every pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit conventions,
and the pull request checklist.

## License

[MIT](LICENSE)
