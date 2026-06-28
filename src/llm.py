"""LLM orchestration: structured brainstorming, JSON sanitization, robust parsing.

Supports two backends:
  - Ollama  (default, local, no API key needed)
  - Anthropic Claude  (fallback when ANTHROPIC_API_KEY is set and Ollama is down)
"""

import json
import os
import re
from typing import Any, Protocol, runtime_checkable

import httpx
import ollama
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.utils import sanitize_slug

DEFAULT_MODEL: str = "llama3"
ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"
FALLBACK_API_ENV: str = "ANTHROPIC_API_KEY"

# Rendered by the CLI layer when the local engine is unreachable.
OLLAMA_TROUBLESHOOTING: str = (
    "[!] Connection to Ollama failed.\n"
    "Is the Ollama application running on your system?\n"
    "To resolve this, please open Ollama or run: `ollama run llama3` in another terminal.\n"
    f"(Set ${FALLBACK_API_ENV} to use Anthropic Claude as a fallback.)"
)


class OllamaConnectionError(RuntimeError):
    """Raised when the local Ollama engine cannot be reached."""


class LLMParseError(ValueError):
    """Raised when the model response cannot be sanitized into the expected schema."""


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


class CategoryItem(BaseModel):
    """A single brainstormed category with a safe folder slug and search intent."""

    display_name: str
    search_query: str
    folder_slug: str

    @field_validator("folder_slug")
    @classmethod
    def _enforce_safe_slug(cls, value: str) -> str:
        safe = sanitize_slug(value)
        if not safe:
            raise ValueError("folder_slug reduced to empty after sanitization")
        return safe


class BrainstormResult(BaseModel):
    """Top-level structured payload returned by the planning model."""

    total_expected_categories: int = Field(ge=0)
    items: list[CategoryItem] = Field(min_length=1)
    image_type_filter: str = Field(default="")
    per_category_counts: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON sanitization
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _extract_json_block(raw: str) -> str:
    """Strip markdown fences and isolate the outermost JSON object."""
    text = raw.strip()

    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMParseError("No JSON object found in model response.")

    return text[start : end + 1]


def parse_brainstorm_response(raw: str) -> BrainstormResult:
    """Sanitize a raw model string and validate it against the schema contract."""
    cleaned = _extract_json_block(raw)

    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"Model returned malformed JSON: {exc}") from exc

    try:
        return BrainstormResult.model_validate(data)
    except ValidationError as exc:
        raise LLMParseError(f"Model JSON did not match schema: {exc}") from exc


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_prompt(goal: str, category_count: int) -> str:
    """Construct a strict JSON-only planning prompt."""
    return (
        "You are a planning engine for an image-harvesting agent. The user wants "
        f'images for the goal: "{goal}".\n\n'
        f"Brainstorm exactly {category_count} distinct, specific categories.\n"
        "For each category provide:\n"
        '  - "display_name": a short human-readable name.\n'
        '  - "search_query": a vivid, detailed image search query.\n'
        '  - "folder_slug": a lowercase snake_case slug (letters, digits, underscores only).\n\n'
        "Optionally include at the top level:\n"
        '  - "image_type_filter": a suggested filter string (e.g. "photo", "illustration", "").\n'
        '  - "per_category_counts": an object mapping folder_slug to suggested image count.\n\n'
        "Respond with ONLY a JSON object, no markdown, no commentary, matching:\n"
        "{\n"
        f'  "total_expected_categories": {category_count},\n'
        '  "image_type_filter": "",\n'
        '  "per_category_counts": {},\n'
        '  "items": [\n'
        '    {"display_name": "Samosa", "search_query": "authentic golden crispy samosa close up",'
        ' "folder_slug": "samosa"}\n'
        "  ]\n"
        "}"
    )


# ---------------------------------------------------------------------------
# Planner protocol + implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class Planner(Protocol):
    def plan(self, goal: str, category_count: int) -> BrainstormResult: ...


class OllamaPlanner:
    """Calls the local Ollama daemon."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model

    def plan(self, goal: str, category_count: int) -> BrainstormResult:
        prompt = _build_prompt(goal, category_count)
        try:
            response = ollama.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                format="json",
            )
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            ConnectionError,
            ollama.RequestError,
        ) as exc:
            raise OllamaConnectionError(OLLAMA_TROUBLESHOOTING) from exc
        except ollama.ResponseError as exc:
            raise OllamaConnectionError(
                f"{OLLAMA_TROUBLESHOOTING}\n(Ollama responded with an error: {exc})"
            ) from exc

        content = response.get("message", {}).get("content", "")
        return parse_brainstorm_response(content)


class AnthropicPlanner:
    """Calls the Anthropic Messages API using httpx (no SDK dependency)."""

    _API_URL = "https://api.anthropic.com/v1/messages"
    _API_VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str = ANTHROPIC_MODEL) -> None:
        self._api_key = api_key
        self._model = model

    def plan(self, goal: str, category_count: int) -> BrainstormResult:
        prompt = _build_prompt(goal, category_count)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._API_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = httpx.post(self._API_URL, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise OllamaConnectionError(
                "Anthropic API unreachable — check your network connection."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMParseError(
                f"Anthropic API returned HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc

        data = resp.json()
        # Response shape: {"content": [{"type": "text", "text": "..."}], ...}
        content_blocks = data.get("content", [])
        if not content_blocks:
            raise LLMParseError("Anthropic API returned an empty content array.")
        text = content_blocks[0].get("text", "")
        return parse_brainstorm_response(text)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_planner(model: str = DEFAULT_MODEL) -> Planner:
    """Return the best available planner.

    Tries Ollama first. If Ollama is unreachable AND ANTHROPIC_API_KEY is set,
    falls back to AnthropicPlanner. Otherwise re-raises OllamaConnectionError.
    """
    ollama_planner = OllamaPlanner(model=model)
    # Probe Ollama by attempting to list local models (cheap, no generation).
    try:
        ollama.list()
        return ollama_planner
    except (
        httpx.ConnectError,
        httpx.TimeoutException,
        ConnectionError,
        ollama.RequestError,
        ollama.ResponseError,
    ):
        pass

    api_key = os.environ.get(FALLBACK_API_ENV, "")
    if api_key:
        return AnthropicPlanner(api_key=api_key)

    # No fallback available — raise so the caller can show the troubleshooting msg.
    raise OllamaConnectionError(OLLAMA_TROUBLESHOOTING)


# ---------------------------------------------------------------------------
# Public API (backward-compatible)
# ---------------------------------------------------------------------------


def brainstorm_categories(
    goal: str,
    category_count: int,
    model: str = DEFAULT_MODEL,
    planner: Planner | None = None,
) -> BrainstormResult:
    """Ask the configured planner to brainstorm categories for *goal*.

    Raises:
        OllamaConnectionError: if no backend is reachable.
        LLMParseError: if the response cannot be parsed into the schema.
    """
    if planner is None:
        planner = build_planner(model=model)
    return planner.plan(goal, category_count)
