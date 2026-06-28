"""Optional image relevance/quality scoring stage.

Provides a pluggable ``Scorer`` protocol so a local CLIP model (or any future
scorer) can be wired into the download pipeline without making the dependency
mandatory. The ``NullScorer`` is the default — a strict no-op that adds zero
overhead. ``ClipScorer`` is only instantiated when the ``clip`` package is
importable; otherwise ``build_scorer`` falls back to ``NullScorer`` silently.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Scorer(Protocol):
    """Score an image against a text query.

    Returns a float in [0, 1]. Higher means more relevant / higher quality.
    Implementations must be synchronous (called from a thread pool inside the
    async pipeline via ``asyncio.to_thread``).
    """

    def score(self, image_bytes: bytes, query: str) -> float: ...


class NullScorer:
    """Passthrough scorer that always returns 1.0 — no scoring applied."""

    def score(self, image_bytes: bytes, query: str) -> float:  # noqa: ARG002
        return 1.0


class ClipScorer:
    """CLIP-based cosine-similarity scorer (optional dep: ``pip install clip``).

    Encodes the image and text query with OpenAI's CLIP and returns their
    cosine similarity normalised to [0, 1]. Lazy-loads the model on first
    call so import time stays cheap.
    """

    def __init__(self, model_name: str = "ViT-B/32", device: str = "cpu") -> None:
        self._model_name = model_name
        self._device = device
        self._model = None
        self._preprocess = None
        self._clip = None

    def _load(self) -> None:
        if self._model is not None:
            return
        import clip  # type: ignore[import]
        import torch  # type: ignore[import]

        self._clip = clip
        self._torch = torch
        model, preprocess = clip.load(self._model_name, device=self._device)
        self._model = model
        self._preprocess = preprocess

    def score(self, image_bytes: bytes, query: str) -> float:
        import io

        from PIL import Image

        self._load()
        clip = self._clip
        torch = self._torch

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image_input = self._preprocess(image).unsqueeze(0).to(self._device)  # type: ignore[misc]
        text_input = clip.tokenize([query]).to(self._device)  # type: ignore[attr-defined]

        with torch.no_grad():
            image_features = self._model.encode_image(image_input)  # type: ignore[attr-defined]
            text_features = self._model.encode_text(text_input)  # type: ignore[attr-defined]
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            similarity = (image_features @ text_features.T).item()

        # cosine similarity in [-1, 1] → normalise to [0, 1]
        return float((similarity + 1.0) / 2.0)


def build_scorer(scorer_name: str = "") -> Scorer:
    """Return a ``Scorer`` instance for *scorer_name*.

    - ``""`` or ``"none"`` → ``NullScorer`` (no-op, zero overhead)
    - ``"clip"`` → ``ClipScorer`` if ``clip`` is importable, else ``NullScorer``
      with a logged warning
    - Any other value → ``NullScorer`` with a logged warning
    """
    name = (scorer_name or "").strip().lower()
    if not name or name == "none":
        return NullScorer()

    if name == "clip":
        try:
            import clip  # type: ignore[import]  # noqa: F401

            return ClipScorer()
        except ImportError:
            logger.warning(
                "scorer='clip' requested but the 'clip' package is not installed. "
                "Falling back to NullScorer. Install with: pip install git+https://github.com/openai/CLIP.git"
            )
            return NullScorer()

    logger.warning("Unknown scorer %r — falling back to NullScorer.", scorer_name)
    return NullScorer()
