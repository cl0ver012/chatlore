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
