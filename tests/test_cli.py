"""Tests for the command-line interface."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore import __version__
from chatlore.cli import app, default_home, load_env_file, run

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
    monkeypatch.delenv("CHATLORE_LLM_REASONING", raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    missing = runner.invoke(app, ["doctor"])
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")

    present = runner.invoke(app, ["doctor"])

    assert "deepseek/deepseek-v4-flash" in missing.output
    assert "reasoning off" in missing.output
    assert "set OPENROUTER_API_KEY" in missing.output
    assert "API key set" in present.output
    assert "sk-or-secret" not in present.output


def test_display_path_shortens_the_home_directory(tmp_path: Path) -> None:
    from chatlore.cli import display_path

    assert display_path(Path.home() / ".chatlore" / "x") == "~/.chatlore/x"
    assert display_path(tmp_path) == str(tmp_path) or display_path(tmp_path).startswith("~/")


@pytest.fixture
def env_file_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the variables these tests read, and restore them afterwards.

    load_dotenv writes straight into os.environ, so each variable is set once
    through monkeypatch first; that way teardown removes whatever a test loaded.
    """
    for name in ("OPENROUTER_API_KEY", "CHATLORE_LLM_MODEL"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


@pytest.mark.usefixtures("env_file_variables")
def test_env_file_fills_in_missing_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-file\n", encoding="utf-8")
    nested = tmp_path / "some" / "folder"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    load_env_file()

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-file"


@pytest.mark.usefixtures("env_file_variables")
def test_the_environment_wins_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-file\nCHATLORE_LLM_MODEL=from-file\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHATLORE_LLM_MODEL", "from-shell")

    load_env_file()

    assert os.environ["CHATLORE_LLM_MODEL"] == "from-shell"
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-file"


@pytest.mark.usefixtures("env_file_variables")
def test_no_env_file_is_fine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("chatlore.cli.find_dotenv", lambda usecwd: "")

    load_env_file()

    assert "OPENROUTER_API_KEY" not in os.environ


@pytest.mark.usefixtures("env_file_variables")
def test_the_chatlore_command_reads_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "200")
    for name in ("CHATLORE_LLM_BASE_URL", "CHATLORE_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.argv", ["chatlore", "doctor"])

    with pytest.raises(SystemExit) as exited:
        run()

    output = capsys.readouterr().out
    assert exited.value.code == 0
    assert "API key set" in output
    assert "sk-or-from-file" not in output
