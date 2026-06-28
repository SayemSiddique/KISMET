"""Shared pytest fixtures."""

import pytest

from src.llm import OllamaConnectionError


@pytest.fixture(autouse=True)
def mock_brainstorm_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: make Ollama appear unavailable so existing tests fall through to manual input."""
    import src.cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "brainstorm_categories",
        lambda **_kwargs: (_ for _ in ()).throw(OllamaConnectionError("offline")),
    )
