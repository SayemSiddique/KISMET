"""Tests for packaging artifacts: pyproject.toml, Dockerfile, Homebrew formula, PyInstaller spec."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# pyproject.toml
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def test_pyproject_parses():
    data = _load_pyproject()
    assert isinstance(data, dict)


def test_pyproject_required_fields():
    proj = _load_pyproject()["project"]
    for field in ("name", "version", "description", "readme", "requires-python", "license"):
        assert field in proj, f"missing field: {field}"


def test_pyproject_name():
    proj = _load_pyproject()["project"]
    assert proj["name"] == "kismet-harvest"


def test_pyproject_classifiers_present():
    proj = _load_pyproject()["project"]
    classifiers = proj.get("classifiers", [])
    assert len(classifiers) >= 5
    assert any("Python :: 3.12" in c for c in classifiers)


def test_pyproject_keywords_present():
    proj = _load_pyproject()["project"]
    keywords = proj.get("keywords", [])
    assert len(keywords) >= 3


def test_pyproject_entry_point():
    data = _load_pyproject()
    scripts = data["project"].get("scripts", {})
    assert "kismet" in scripts
    assert scripts["kismet"] == "src.cli:app"


def test_pyproject_extras_defined():
    data = _load_pyproject()
    extras = data["project"].get("optional-dependencies", {})
    for extra in ("vision", "bg", "web", "all"):
        assert extra in extras, f"missing extra: {extra}"


def test_pyproject_extras_non_empty():
    data = _load_pyproject()
    extras = data["project"]["optional-dependencies"]
    for name, deps in extras.items():
        assert len(deps) >= 1, f"extra [{name}] has no deps"


def test_pyproject_urls_present():
    proj = _load_pyproject()["project"]
    urls = proj.get("urls", {})
    assert len(urls) >= 1


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


def _dockerfile_text() -> str:
    return (REPO_ROOT / "Dockerfile").read_text()


def test_dockerfile_exists():
    assert (REPO_ROOT / "Dockerfile").exists()


def test_dockerfile_base_image():
    text = _dockerfile_text()
    assert "python:3.12" in text


def test_dockerfile_exposes_8000():
    text = _dockerfile_text()
    assert "EXPOSE 8000" in text


def test_dockerfile_uvicorn_cmd():
    text = _dockerfile_text()
    assert "uvicorn" in text
    assert "src.web:build_app" in text
    assert "--factory" in text


# ---------------------------------------------------------------------------
# Homebrew formula
# ---------------------------------------------------------------------------


def _formula_text() -> str:
    return (REPO_ROOT / "Formula" / "kismet.rb").read_text()


def test_formula_exists():
    assert (REPO_ROOT / "Formula" / "kismet.rb").exists()


def test_formula_class_declaration():
    text = _formula_text()
    assert "class Kismet < Formula" in text


def test_formula_depends_python():
    text = _formula_text()
    assert 'depends_on "python@3.12"' in text


def test_formula_has_test_block():
    text = _formula_text()
    assert "test do" in text


# ---------------------------------------------------------------------------
# PyInstaller spec
# ---------------------------------------------------------------------------


def _spec_text() -> str:
    return (REPO_ROOT / "kismet.spec").read_text()


def test_spec_exists():
    assert (REPO_ROOT / "kismet.spec").exists()


def test_spec_has_analysis():
    assert "Analysis(" in _spec_text()


def test_spec_has_exe():
    assert "EXE(" in _spec_text()


def test_spec_targets_cli():
    assert "src/cli.py" in _spec_text()
