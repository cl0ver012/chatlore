"""Tests for the command-line interface."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore import __version__
from chatlore.cli import app, default_home

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"chatlore {__version__}"


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output


def test_default_home_falls_back_to_user_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHATLORE_HOME", raising=False)

    assert default_home() == Path.home() / ".chatlore"


def test_default_home_honours_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path))

    assert default_home() == tmp_path


def test_doctor_reports_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path))

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "python" in result.output
    assert "exists" in result.output


def test_doctor_names_the_model_without_showing_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path))
    monkeypatch.delenv("CHATLORE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("CHATLORE_LLM_MODEL", raising=False)
    monkeypatch.delenv("CHATLORE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    missing = runner.invoke(app, ["doctor"])
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")

    present = runner.invoke(app, ["doctor"])

    assert "z-ai/glm-5.3-flash" in missing.output
    assert "set OPENROUTER_API_KEY" in missing.output
    assert "API key set" in present.output
    assert "sk-or-secret" not in present.output


def test_display_path_shortens_the_home_directory(tmp_path: Path) -> None:
    from chatlore.cli import display_path

    assert display_path(Path.home() / ".chatlore" / "x") == "~/.chatlore/x"
    assert display_path(tmp_path) == str(tmp_path) or display_path(tmp_path).startswith("~/")
