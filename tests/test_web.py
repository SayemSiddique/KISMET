"""Tests for FastAPI web backend including /api/brainstorm."""


import pytest
from fastapi.testclient import TestClient

import src.web as web_mod
from src.llm import BrainstormResult, CategoryItem, OllamaConnectionError


def _make_result() -> BrainstormResult:
    return BrainstormResult(
        total_expected_categories=2,
        items=[
            CategoryItem(display_name="Starters", search_query="tasty starters", folder_slug="starters"),
            CategoryItem(display_name="Mains", search_query="main course dishes", folder_slug="mains"),
        ],
    )


@pytest.fixture()
def client() -> TestClient:
    from src.web import build_app
    return TestClient(build_app())


class TestBrainstormRoute:
    def test_success_returns_items(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(web_mod, "brainstorm_categories", lambda **_: _make_result())
        resp = client.post("/api/brainstorm", json={"goal": "Indian food", "count": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["items"][0]["display_name"] == "Starters"

    def test_ollama_unavailable_returns_503(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            web_mod,
            "brainstorm_categories",
            lambda **_: (_ for _ in ()).throw(OllamaConnectionError("offline")),
        )
        resp = client.post("/api/brainstorm", json={"goal": "Cars", "count": 5})
        assert resp.status_code == 503
        assert "error" in resp.json()

    def test_missing_goal_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/brainstorm", json={"count": 5})
        assert resp.status_code == 422
