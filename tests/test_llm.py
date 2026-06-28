"""Tests for src/llm.py — Planner protocol, backends, fallback logic, parsing."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.llm import (
    FALLBACK_API_ENV,
    AnthropicPlanner,
    BrainstormResult,
    LLMParseError,
    OllamaConnectionError,
    OllamaPlanner,
    Planner,
    brainstorm_categories,
    build_planner,
    parse_brainstorm_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result_payload(
    *,
    image_type_filter: str = "",
    per_category_counts: dict | None = None,
) -> str:
    return json.dumps(
        {
            "total_expected_categories": 2,
            "image_type_filter": image_type_filter,
            "per_category_counts": per_category_counts or {},
            "items": [
                {"display_name": "Cats", "search_query": "fluffy cats", "folder_slug": "cats"},
                {"display_name": "Dogs", "search_query": "happy dogs", "folder_slug": "dogs"},
            ],
        }
    )


# ---------------------------------------------------------------------------
# parse_brainstorm_response
# ---------------------------------------------------------------------------


class TestParseBrainstormResponse:
    def test_parses_minimal_payload(self):
        raw = json.dumps(
            {
                "total_expected_categories": 1,
                "items": [{"display_name": "X", "search_query": "x", "folder_slug": "x"}],
            }
        )
        result = parse_brainstorm_response(raw)
        assert isinstance(result, BrainstormResult)
        assert result.image_type_filter == ""
        assert result.per_category_counts == {}

    def test_parses_extended_payload(self):
        raw = _make_result_payload(
            image_type_filter="photo",
            per_category_counts={"cats": 15, "dogs": 10},
        )
        result = parse_brainstorm_response(raw)
        assert result.image_type_filter == "photo"
        assert result.per_category_counts == {"cats": 15, "dogs": 10}

    def test_strips_markdown_fence(self):
        inner = _make_result_payload()
        raw = f"```json\n{inner}\n```"
        result = parse_brainstorm_response(raw)
        assert len(result.items) == 2

    def test_raises_on_missing_json(self):
        with pytest.raises(LLMParseError, match="No JSON object"):
            parse_brainstorm_response("no json here")

    def test_raises_on_bad_json(self):
        with pytest.raises(LLMParseError, match="malformed JSON"):
            parse_brainstorm_response("{bad json}")

    def test_raises_on_schema_mismatch(self):
        with pytest.raises(LLMParseError, match="did not match schema"):
            parse_brainstorm_response(json.dumps({"items": []}))


# ---------------------------------------------------------------------------
# Planner protocol
# ---------------------------------------------------------------------------


class TestPlannerProtocol:
    def test_ollama_planner_is_planner(self):
        assert isinstance(OllamaPlanner(), Planner)

    def test_anthropic_planner_is_planner(self):
        assert isinstance(AnthropicPlanner(api_key="fake"), Planner)


# ---------------------------------------------------------------------------
# OllamaPlanner
# ---------------------------------------------------------------------------


class TestOllamaPlanner:
    def test_plan_success(self):
        payload = _make_result_payload()
        mock_response = {"message": {"content": payload}}
        with patch("src.llm.ollama.chat", return_value=mock_response):
            result = OllamaPlanner().plan("cats and dogs", 2)
        assert len(result.items) == 2
        assert result.items[0].folder_slug == "cats"

    def test_plan_raises_on_connect_error(self):
        import httpx

        with (
            patch("src.llm.ollama.chat", side_effect=httpx.ConnectError("refused")),
            pytest.raises(OllamaConnectionError),
        ):
            OllamaPlanner().plan("goal", 2)

    def test_plan_raises_on_connection_error(self):
        with (
            patch("src.llm.ollama.chat", side_effect=ConnectionError("down")),
            pytest.raises(OllamaConnectionError),
        ):
            OllamaPlanner().plan("goal", 2)


# ---------------------------------------------------------------------------
# AnthropicPlanner
# ---------------------------------------------------------------------------


class TestAnthropicPlanner:
    def _mock_response(self, payload: str):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": payload}]}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def test_plan_success(self):
        payload = _make_result_payload(image_type_filter="illustration")
        with patch("src.llm.httpx.post", return_value=self._mock_response(payload)):
            result = AnthropicPlanner(api_key="sk-test").plan("goal", 2)
        assert result.image_type_filter == "illustration"
        assert len(result.items) == 2

    def test_plan_uses_correct_headers(self):
        payload = _make_result_payload()
        with patch("src.llm.httpx.post", return_value=self._mock_response(payload)) as mock_post:
            AnthropicPlanner(api_key="sk-abc").plan("goal", 2)
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["x-api-key"] == "sk-abc"
        assert kwargs["headers"]["anthropic-version"] == "2023-06-01"

    def test_plan_uses_configured_model(self):
        payload = _make_result_payload()
        with patch("src.llm.httpx.post", return_value=self._mock_response(payload)) as mock_post:
            AnthropicPlanner(api_key="sk-abc", model="claude-opus-4-8").plan("goal", 2)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["model"] == "claude-opus-4-8"

    def test_plan_raises_on_http_error(self):
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with (
            patch(
                "src.llm.httpx.post",
                side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=mock_resp),
            ),
            pytest.raises(LLMParseError, match="401"),
        ):
            AnthropicPlanner(api_key="bad").plan("goal", 2)

    def test_plan_raises_on_empty_content(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": []}
        mock_resp.raise_for_status.return_value = None
        with (
            patch("src.llm.httpx.post", return_value=mock_resp),
            pytest.raises(LLMParseError, match="empty content"),
        ):
            AnthropicPlanner(api_key="sk-test").plan("goal", 2)

    def test_plan_raises_on_network_error(self):
        import httpx

        with (
            patch("src.llm.httpx.post", side_effect=httpx.ConnectError("refused")),
            pytest.raises(OllamaConnectionError, match="unreachable"),
        ):
            AnthropicPlanner(api_key="sk-test").plan("goal", 2)


# ---------------------------------------------------------------------------
# build_planner — factory / fallback logic
# ---------------------------------------------------------------------------


class TestBuildPlanner:
    def test_returns_ollama_when_available(self):
        with patch("src.llm.ollama.list", return_value={}):
            planner = build_planner()
        assert isinstance(planner, OllamaPlanner)

    def test_falls_back_to_anthropic_when_ollama_down_and_key_set(self, monkeypatch):
        import httpx

        monkeypatch.setenv(FALLBACK_API_ENV, "sk-test")
        with patch("src.llm.ollama.list", side_effect=httpx.ConnectError("down")):
            planner = build_planner()
        assert isinstance(planner, AnthropicPlanner)

    def test_raises_when_ollama_down_and_no_key(self, monkeypatch):
        monkeypatch.delenv(FALLBACK_API_ENV, raising=False)
        with (
            patch("src.llm.ollama.list", side_effect=ConnectionError("down")),
            pytest.raises(OllamaConnectionError),
        ):
            build_planner()

    def test_raises_when_ollama_response_error_and_no_key(self, monkeypatch):
        import ollama

        monkeypatch.delenv(FALLBACK_API_ENV, raising=False)
        with (
            patch("src.llm.ollama.list", side_effect=ollama.ResponseError("err")),
            pytest.raises(OllamaConnectionError),
        ):
            build_planner()


# ---------------------------------------------------------------------------
# brainstorm_categories — public API, backward compat
# ---------------------------------------------------------------------------


class TestBrainstormCategories:
    def test_uses_provided_planner(self):
        payload = _make_result_payload()
        mock_planner = MagicMock()
        mock_planner.plan.return_value = parse_brainstorm_response(payload)
        result = brainstorm_categories("goal", 2, planner=mock_planner)
        mock_planner.plan.assert_called_once_with("goal", 2)
        assert len(result.items) == 2

    def test_backward_compat_no_extended_fields(self):
        raw = json.dumps(
            {
                "total_expected_categories": 1,
                "items": [{"display_name": "X", "search_query": "x", "folder_slug": "x"}],
            }
        )
        result = parse_brainstorm_response(raw)
        assert result.image_type_filter == ""
        assert result.per_category_counts == {}

    def test_calls_build_planner_when_no_planner_given(self):
        payload = _make_result_payload()
        mock_planner = MagicMock()
        mock_planner.plan.return_value = parse_brainstorm_response(payload)
        with patch("src.llm.build_planner", return_value=mock_planner):
            result = brainstorm_categories("goal", 2)
        assert len(result.items) == 2
