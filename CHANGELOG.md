# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
