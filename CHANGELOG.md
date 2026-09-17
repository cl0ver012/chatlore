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
