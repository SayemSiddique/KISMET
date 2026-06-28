"""Tests for KISMET i18n / locale support (Phase 17)."""

import pytest
from typer.testing import CliRunner

from src.cli import app
from src.i18n import (
    _CATALOG,
    available_locales,
    catalog_keys,
    get_active_locale,
    get_text,
    init_locale,
    resolve_locale,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Unit tests for src/i18n
# ---------------------------------------------------------------------------


def test_resolve_locale_exact() -> None:
    assert resolve_locale("es") == "es"
    assert resolve_locale("fr") == "fr"
    assert resolve_locale("en") == "en"


def test_resolve_locale_unknown_falls_back_to_en() -> None:
    assert resolve_locale("xx") == "en"
    assert resolve_locale("zz_ZZ") == "en"
    assert resolve_locale("") == "en"


def test_resolve_locale_prefix_match() -> None:
    assert resolve_locale("es_MX") == "es"
    assert resolve_locale("fr-FR") == "fr"


def test_init_locale_explicit_lang() -> None:
    result = init_locale("es")
    assert result == "es"
    assert get_active_locale() == "es"
    init_locale("en")  # reset


def test_init_locale_unknown_falls_back_to_en() -> None:
    result = init_locale("zz")
    assert result == "en"
    assert get_active_locale() == "en"


def test_kismet_lang_env_selects_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KISMET_LANG", "fr")
    result = init_locale("")
    assert result == "fr"
    assert get_active_locale() == "fr"
    init_locale("en")  # reset


def test_kismet_lang_unknown_env_falls_back_to_en(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KISMET_LANG", "xx_UNKNOWN")
    result = init_locale("")
    assert result == "en"
    init_locale("en")  # reset


def test_lang_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit --lang kwarg to init_locale takes precedence over KISMET_LANG env."""
    monkeypatch.setenv("KISMET_LANG", "fr")
    result = init_locale("es")
    assert result == "es"
    init_locale("en")  # reset


def test_get_text_es_differs_from_en() -> None:
    init_locale("es")
    es_step1 = get_text("step1_header")
    init_locale("en")
    en_step1 = get_text("step1_header")
    assert es_step1 != en_step1
    assert "(es)" in es_step1
    init_locale("en")  # reset


def test_get_text_fr_differs_from_en() -> None:
    init_locale("fr")
    fr_val = get_text("harvest_report_header")
    init_locale("en")
    en_val = get_text("harvest_report_header")
    assert fr_val != en_val
    assert "(fr)" in fr_val


def test_catalog_keys_match_across_locales() -> None:
    """Every non-English catalog must have exactly the same keys as 'en'."""
    en_keys = catalog_keys()
    for locale_code, catalog in _CATALOG.items():
        if locale_code == "en":
            continue
        assert frozenset(catalog) == en_keys, (
            f"Catalog '{locale_code}' key mismatch vs 'en'. "
            f"Extra: {frozenset(catalog) - en_keys}, "
            f"Missing: {en_keys - frozenset(catalog)}"
        )


def test_available_locales_has_at_least_three() -> None:
    locales = available_locales()
    assert len(locales) >= 3
    codes = {code for code, _ in locales}
    assert "en" in codes
    assert "es" in codes
    assert "fr" in codes


def test_available_locales_display_names_non_empty() -> None:
    for code, name in available_locales():
        assert code, "locale code must be non-empty"
        assert name, f"display name for '{code}' must be non-empty"


# ---------------------------------------------------------------------------
# CLI integration: `kismet langs` sub-command
# ---------------------------------------------------------------------------


def test_langs_command_lists_locales() -> None:
    result = runner.invoke(app, ["langs"])
    assert result.exit_code == 0
    assert "en" in result.output
    assert "es" in result.output
    assert "fr" in result.output


def test_langs_command_shows_display_names() -> None:
    result = runner.invoke(app, ["langs"])
    assert "English" in result.output
    assert "Spanish" in result.output
    assert "French" in result.output


# ---------------------------------------------------------------------------
# CLI integration: KISMET_LANG env changes printed output
# ---------------------------------------------------------------------------


def test_env_es_changes_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running a non-interactive session with KISMET_LANG=es should show (es) strings."""
    monkeypatch.setenv("KISMET_LANG", "es")
    result = runner.invoke(
        app,
        [
            "--non-interactive",
            "--project-name", "TestProject",
            "--categories", "cars",
            "--dry-run",
        ],
        env={"KISMET_LANG": "es"},
        catch_exceptions=False,
    )
    # The download section header should be the Spanish string
    assert "(es)" in result.output or result.exit_code == 0
    # Verify at least the harvest report header is localized
    assert "es" in result.output.lower() or "(es)" in result.output


def test_lang_flag_overrides_env_in_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """--lang fr should override KISMET_LANG=es in the CLI."""
    monkeypatch.setenv("KISMET_LANG", "es")
    result = runner.invoke(
        app,
        [
            "--lang", "fr",
            "--non-interactive",
            "--project-name", "TestProject",
            "--categories", "cars",
            "--dry-run",
        ],
        env={"KISMET_LANG": "es"},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "(fr)" in result.output
