# Contributing to ChatLore

Thanks for taking a look. The project is early, so the most useful
contributions right now are issues that describe real export files that fail
to import, and pull requests that follow the roadmap in the README.

## Development setup

```bash
uv sync
uv run chatlore --help
```

Run the same checks CI runs before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

`uv run ruff format .` fixes formatting in place.

If uv cannot reach the network on your machine, a plain virtual environment
works too and the checks are the same:

```bash
python3.12 -m venv .venv
.venv/Scripts/python -m pip install -e . --group dev   # Windows
# .venv/bin/python -m pip install -e . --group dev     # macOS / Linux
```

## Branches

- `main` is protected. Every change lands through a pull request with green CI.
- Branch names: `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `chore/<topic>`.

## Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(importers): add ChatGPT export parser
fix(store): handle empty conversations on upsert
docs: describe the Gemini Takeout format
```

Keep one logical change per commit. The body explains why, not what. The only
footer used is `Closes #<issue>` when a commit resolves an issue.

## Pull requests

- Keep a pull request small enough to review in about twenty minutes.
- Fill in the pull request template: summary, changes, how it was tested.
- Squash merge is used, so the pull request title becomes the commit message
  on `main` and must itself be a valid Conventional Commit line.

## Privacy

Never commit real chat exports, databases, or API keys. Test fixtures are
synthetic and live under `tests/fixtures/`. The `.gitignore` already excludes
`exports/`, `.chatlore/`, and `.env` files.
