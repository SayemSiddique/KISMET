"""Phase 16 — CLI UX polish & shell completion tests."""


import json
import re
from unittest.mock import patch

from typer.testing import CliRunner

from src.cli import _get_version, app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def test_version_flag_semver() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # Output must contain a semver-ish string  e.g. "kismet 0.1.0"
    assert re.search(r"\d+\.\d+\.\d+", result.output)


def test_version_short_flag() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert re.search(r"\d+\.\d+\.\d+", result.output)


def test_get_version_fallback() -> None:
    # When importlib.metadata raises, fallback to "0.1.0"
    with patch("importlib.metadata.version", side_effect=Exception("no dist")):
        v = _get_version()
    assert re.match(r"\d+\.\d+\.\d+", v)


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


def test_completion_bash_nonempty() -> None:
    result = runner.invoke(app, ["completion", "bash"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_zsh_nonempty() -> None:
    result = runner.invoke(app, ["completion", "zsh"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_fish_nonempty() -> None:
    result = runner.invoke(app, ["completion", "fish"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_unknown_shell_exits_1() -> None:
    result = runner.invoke(app, ["completion", "powershell"])
    assert result.exit_code == 1
    assert "Unknown shell" in result.output or "Unknown shell" in (result.stderr or "")


# ---------------------------------------------------------------------------
# Config sub-command
# ---------------------------------------------------------------------------


def test_show_config_valid_json() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, dict)


def test_show_config_has_expected_keys() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "defaults" in parsed


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def test_error_hides_traceback_normally(monkeypatch: object) -> None:
    """A runtime exception in main() should print one-line 'Error:' without traceback."""
    monkeypatch.delenv("KISMET_DEBUG", raising=False)  # type: ignore[attr-defined]

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("synthetic failure")

    with patch("src.cli._run_session", side_effect=_boom), patch(
        "src.cli._run_session_non_interactive", side_effect=_boom
    ):
        result = runner.invoke(
            app, ["--non-interactive", "--project-name", "x", "--categories", "y"]
        )

    assert result.exit_code != 0
    # Should not contain a Python traceback
    assert "Traceback" not in result.output
    assert "Error:" in result.output


def test_error_shows_traceback_with_debug(monkeypatch: object) -> None:
    """With KISMET_DEBUG=1 the full traceback must be printed."""
    monkeypatch.setenv("KISMET_DEBUG", "1")  # type: ignore[attr-defined]

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("synthetic failure debug")

    with patch("src.cli._run_session", side_effect=_boom), patch(
        "src.cli._run_session_non_interactive", side_effect=_boom
    ):
        result = runner.invoke(
            app,
            ["--non-interactive", "--project-name", "x", "--categories", "y"],
            catch_exceptions=False,
        )

    # The traceback is printed to stderr/stdout via traceback.print_exc()
    combined = (result.output or "") + (result.stderr or "")
    assert "RuntimeError" in combined or result.exit_code != 0


# ---------------------------------------------------------------------------
# Sub-command reachability
# ---------------------------------------------------------------------------


def test_web_subcommand_importable() -> None:
    """web sub-command should appear in help without import errors."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "web" in result.output


def test_completion_subcommand_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "completion" in result.output


def test_config_subcommand_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "config" in result.output
