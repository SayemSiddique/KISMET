"""Quality-gate tests: verify CI config files are present and well-formed."""


import tomllib
from pathlib import Path

REPO = Path(__file__).parent.parent
PYPROJECT = REPO / "pyproject.toml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
PRE_COMMIT = REPO / ".pre-commit-config.yaml"
CHANGELOG = REPO / "CHANGELOG.md"


# ---------------------------------------------------------------------------
# pyproject.toml — tool sections
# ---------------------------------------------------------------------------


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def test_pyproject_has_ruff_section():
    cfg = _pyproject()
    assert "tool" in cfg
    assert "ruff" in cfg["tool"], "[tool.ruff] section missing from pyproject.toml"


def test_pyproject_ruff_has_line_length():
    ruff = _pyproject()["tool"]["ruff"]
    assert "line-length" in ruff, "ruff line-length not set"
    assert ruff["line-length"] == 100


def test_pyproject_ruff_has_target_version():
    ruff = _pyproject()["tool"]["ruff"]
    assert "target-version" in ruff, "ruff target-version not set"
    assert ruff["target-version"] == "py312"


def test_pyproject_has_mypy_section():
    cfg = _pyproject()
    assert "mypy" in cfg["tool"], "[tool.mypy] section missing from pyproject.toml"


def test_pyproject_mypy_ignore_missing_imports():
    mypy = _pyproject()["tool"]["mypy"]
    assert mypy.get("ignore_missing_imports") is True


def test_pyproject_mypy_python_version():
    mypy = _pyproject()["tool"]["mypy"]
    assert mypy.get("python_version") == "3.12"


def test_pyproject_has_dev_extra():
    cfg = _pyproject()
    extras = cfg["project"]["optional-dependencies"]
    assert "dev" in extras, "dev extra missing"
    dev_deps = " ".join(extras["dev"])
    assert "ruff" in dev_deps
    assert "mypy" in dev_deps


# ---------------------------------------------------------------------------
# GitHub Actions CI workflow
# ---------------------------------------------------------------------------


def test_ci_workflow_exists():
    assert CI_WORKFLOW.exists(), f"CI workflow not found at {CI_WORKFLOW}"


def test_ci_workflow_has_python_matrix():
    text = CI_WORKFLOW.read_text()
    assert "python-version" in text, "CI workflow missing python-version matrix"
    assert "3.12" in text
    assert "3.13" in text


def test_ci_workflow_runs_pytest():
    text = CI_WORKFLOW.read_text()
    assert "pytest" in text, "CI workflow does not invoke pytest"


def test_ci_workflow_has_pip_cache():
    text = CI_WORKFLOW.read_text()
    assert "cache" in text.lower(), "CI workflow missing pip cache step"


# ---------------------------------------------------------------------------
# pre-commit config
# ---------------------------------------------------------------------------


def test_pre_commit_config_exists():
    assert PRE_COMMIT.exists(), ".pre-commit-config.yaml not found"


def test_pre_commit_references_ruff():
    text = PRE_COMMIT.read_text()
    assert "ruff" in text, ".pre-commit-config.yaml does not reference ruff"


def test_pre_commit_references_mypy():
    text = PRE_COMMIT.read_text()
    assert "mypy" in text, ".pre-commit-config.yaml does not reference mypy"


# ---------------------------------------------------------------------------
# CHANGELOG
# ---------------------------------------------------------------------------


def test_changelog_exists():
    assert CHANGELOG.exists(), "CHANGELOG.md not found"


def test_changelog_has_unreleased_heading():
    text = CHANGELOG.read_text()
    assert "[Unreleased]" in text, "CHANGELOG.md missing [Unreleased] heading"
