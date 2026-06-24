"""Ollama orchestration: structured brainstorming, JSON sanitization, robust parsing."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import ollama
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.utils import sanitize_slug

DEFAULT_MODEL: str = "llama3"
FALLBACK_API_ENV: str = "ANTHROPIC_API_KEY"

# Rendered by the CLI layer when the local engine is unreachable.
OLLAMA_TROUBLESHOOTING: str = (
    "[!] Connection to Ollama failed.\n"
    "Is the Ollama application running on your system?\n"
    "To resolve this, please open Ollama or run: `ollama run llama3` in another terminal.\n"
    f"(A future release will support an external API fallback via ${FALLBACK_API_ENV}.)"
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
        # The model's output is untrusted; re-sanitize through the security ring.
        safe = sanitize_slug(value)
        if not safe:
            raise ValueError("folder_slug reduced to empty after sanitization")
        return safe


class BrainstormResult(BaseModel):
    """Top-level structured payload returned by the planning model."""

    total_expected_categories: int = Field(ge=0)
    items: list[CategoryItem] = Field(min_length=1)


# ---------------------------------------------------------------------------
# JSON sanitization
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _extract_json_block(raw: str) -> str:
    """Strip markdown fences and isolate the outermost JSON object.

    Open-source models frequently wrap payloads in ```json ... ``` fences or
    prepend conversational text. This normalizes both cases.
    """
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
    """Construct a strict JSON-only planning prompt for the local model."""
    return (
        "You are a planning engine for an image-harvesting agent. The user wants "
        f"images for the goal: \"{goal}\".\n\n"
        f"Brainstorm exactly {category_count} distinct, specific categories.\n"
        "For each category provide:\n"
        '  - "display_name": a short human-readable name.\n'
        '  - "search_query": a vivid, detailed image search query.\n'
        '  - "folder_slug": a lowercase snake_case slug (letters, digits, underscores only).\n\n'
        "Respond with ONLY a JSON object, no markdown, no commentary, matching:\n"
        "{\n"
        f'  "total_expected_categories": {category_count},\n'
        '  "items": [\n'
        '    {"display_name": "Samosa", "search_query": "authentic golden crispy samosa close up", "folder_slug": "samosa"}\n'
        "  ]\n"
        "}"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def brainstorm_categories(
    goal: str,
    category_count: int,
    model: str = DEFAULT_MODEL,
) -> BrainstormResult:
    """Ask the local Ollama model to brainstorm categories for *goal*.

    Raises:
        OllamaConnectionError: if the local engine is unreachable.
        LLMParseError: if the response cannot be parsed into the schema.
    """
    prompt = _build_prompt(goal, category_count)

    try:
        response = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            format="json",
        )
    except (
        httpx.ConnectError,
        httpx.TimeoutException,  # covers connect + read timeouts (hung model)
        ConnectionError,  # the ollama client wraps daemon-down into builtins.ConnectionError
        ollama.RequestError,
    ) as exc:
        raise OllamaConnectionError(OLLAMA_TROUBLESHOOTING) from exc
    except ollama.ResponseError as exc:
        # Model not pulled, or the daemon rejected the request.
        raise OllamaConnectionError(
            f"{OLLAMA_TROUBLESHOOTING}\n(Ollama responded with an error: {exc})"
        ) from exc

    content = response.get("message", {}).get("content", "")
    return parse_brainstorm_response(content)
