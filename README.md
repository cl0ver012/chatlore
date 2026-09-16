# ChatLore

> All your AI conversations, one graph, one chat.

[![CI](https://github.com/cl0ver012/chatlore/actions/workflows/ci.yml/badge.svg)](https://github.com/cl0ver012/chatlore/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**Status: pre-alpha.** This repository currently holds the project skeleton and
the development plan. Nothing is usable yet beyond `chatlore --help`. The
[development plan](docs/DEVELOPMENT_PLAN.md) describes what is coming and in
which order.

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
| M0 | Repository skeleton, CI, development plan | in progress |
| M1 | Core data model and embedded SQLite graph store | planned |
| M2 | Importers: ChatGPT, Claude, Gemini, Markdown, notes | planned |
| M3 | Chunking, embeddings, hybrid search | planned |
| M4 | Entity, topic, and fact extraction with provenance | planned |
| M5 | REST API with streaming chat | planned |
| M6 | Web UI: conversations, graph explorer, chat | planned |
| M7 | MCP server for Claude Desktop, Claude Code, Cursor, ChatGPT | planned |
| M8 | FalkorDB backend | planned |
| M9 | Hosted demo | planned |
| M10 | v0.1.0 release | planned |

Full details, data model, and decisions: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md).

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
