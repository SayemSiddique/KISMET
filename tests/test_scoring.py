"""Tests for src/scoring.py — fully offline (no real clip/torch)."""

import asyncio
import io
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from PIL import Image

from src.scoring import ClipScorer, NullScorer, Scorer, build_scorer

if TYPE_CHECKING:
    from src.downloader import CategoryJob, HarvestReport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_bytes(width: int = 10, height: int = 10) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(128, 64, 32)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# NullScorer
# ---------------------------------------------------------------------------


class TestNullScorer:
    def test_always_returns_one(self):
        s = NullScorer()
        assert s.score(b"anything", "query") == 1.0

    def test_empty_bytes_and_query(self):
        s = NullScorer()
        assert s.score(b"", "") == 1.0

    def test_satisfies_protocol(self):
        assert isinstance(NullScorer(), Scorer)


# ---------------------------------------------------------------------------
# build_scorer
# ---------------------------------------------------------------------------


class TestBuildScorer:
    def test_empty_string_returns_null(self):
        assert isinstance(build_scorer(""), NullScorer)

    def test_none_string_returns_null(self):
        assert isinstance(build_scorer("none"), NullScorer)

    def test_unknown_name_returns_null_with_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="src.scoring"):
            result = build_scorer("imagenet")
        assert isinstance(result, NullScorer)
        assert "Unknown scorer" in caplog.text

    def test_clip_unavailable_returns_null_with_warning(self, caplog):
        import logging

        # Ensure 'clip' is not importable by hiding it from sys.modules
        original = sys.modules.pop("clip", None)
        try:
            with caplog.at_level(logging.WARNING, logger="src.scoring"):
                result = build_scorer("clip")
            assert isinstance(result, NullScorer)
            assert "clip" in caplog.text.lower()
        finally:
            if original is not None:
                sys.modules["clip"] = original

    def test_clip_available_returns_clip_scorer(self):
        fake_clip = types.ModuleType("clip")
        fake_clip.load = MagicMock(return_value=(MagicMock(), MagicMock()))
        fake_clip.tokenize = MagicMock()
        with patch.dict(sys.modules, {"clip": fake_clip}):
            result = build_scorer("clip")
        assert isinstance(result, ClipScorer)


# ---------------------------------------------------------------------------
# ClipScorer with a fully faked clip+torch
# ---------------------------------------------------------------------------


def _make_fake_clip_modules():
    """Return (fake_clip, fake_torch) that simulate the CLIP API — no numpy."""
    fake_torch = types.ModuleType("torch")

    class FakeTensor:
        """Minimal tensor stub sufficient to drive ClipScorer._load + score()."""

        def __init__(self, value: float = 0.8):
            self._value = value

        def unsqueeze(self, dim):
            return self

        def to(self, device):
            return self

        def norm(self, dim=None, keepdim=False):
            return FakeTensor(abs(self._value) or 1.0)

        def __truediv__(self, other):
            v = other._value if isinstance(other, FakeTensor) else other
            return FakeTensor(self._value / (v or 1.0))

        def __matmul__(self, other):
            # Simulate cosine similarity ≈ 0.6 (normalised → 0.8 in [0,1])
            return FakeTensor(0.6)

        @property
        def T(self):
            return self

        def item(self):
            return self._value

    class FakeModel:
        def encode_image(self, x):
            return FakeTensor(0.8)

        def encode_text(self, x):
            return FakeTensor(0.8)

    class FakeNoGrad:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    fake_torch.no_grad = FakeNoGrad

    fake_clip = types.ModuleType("clip")
    fake_clip.load = MagicMock(return_value=(FakeModel(), lambda img: FakeTensor()))
    fake_clip.tokenize = MagicMock(return_value=FakeTensor())

    return fake_clip, fake_torch


class TestClipScorer:
    def test_score_returns_float_in_range(self):
        fake_clip, fake_torch = _make_fake_clip_modules()
        with patch.dict(sys.modules, {"clip": fake_clip, "torch": fake_torch}):
            scorer = ClipScorer()
            result = scorer.score(_make_png_bytes(), "red car photo")
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_score_lazy_loads_model_once(self):
        fake_clip, fake_torch = _make_fake_clip_modules()
        with patch.dict(sys.modules, {"clip": fake_clip, "torch": fake_torch}):
            scorer = ClipScorer()
            scorer.score(_make_png_bytes(), "first call")
            scorer.score(_make_png_bytes(), "second call")
        # clip.load called exactly once across both score() calls
        fake_clip.load.assert_called_once()

    def test_satisfies_protocol(self):
        assert issubclass(ClipScorer, Scorer) or isinstance(ClipScorer(), Scorer)


# ---------------------------------------------------------------------------
# Integration: scorer wired into _harvest_category
# ---------------------------------------------------------------------------


class TestScorerInPipeline:
    """Verify that the scorer gate inside _harvest_category works correctly."""

    def _make_job(self, min_score: float) -> "CategoryJob":
        import tempfile

        from src.downloader import CategoryJob

        tmp = Path(tempfile.mkdtemp())
        return CategoryJob(
            folder_slug="test/item",
            search_query="red apple",
            dest_dir=tmp,
            filenames=["apple_01"],
            min_score=min_score,
        )

    def _run_with_scorer(self, scorer, min_score: float) -> "HarvestReport":
        """Run a single-job harvest with a fake single-URL provider."""
        import httpx

        from src.downloader import DiscoveryResult, harvest

        png = _make_png_bytes()

        class _FakeProvider:
            async def discover_with_meta(self, client, query, count, image_type_filter=""):
                return [DiscoveryResult(url="http://fake/img.png", provider="test")]

        async def _go():
            transport = httpx.MockTransport(
                lambda req: httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
            )
            async with httpx.AsyncClient(transport=transport) as client:
                job = self._make_job(min_score)
                return await harvest(
                    [job],
                    provider=_FakeProvider(),
                    client=client,
                    resume=False,
                    dedup_threshold=0,
                    scorer=scorer if min_score > 0.0 else None,
                )

        return asyncio.run(_go())

    def test_null_scorer_passthrough(self):
        """NullScorer always scores 1.0 → image is kept."""
        scorer = NullScorer()
        report = self._run_with_scorer(scorer, min_score=0.5)
        assert report.total_saved == 1
        assert report.total_filtered == 0

    def test_zero_min_score_no_scorer_called(self):
        """min_score=0.0 means scorer arg is None → image always kept."""
        report = self._run_with_scorer(None, min_score=0.0)
        assert report.total_saved == 1
        assert report.total_filtered == 0

    def test_high_threshold_drops_image(self):
        """A scorer that returns 0.1 with min_score=0.5 should drop the image."""

        class LowScorer:
            def score(self, image_bytes, query):
                return 0.1

        report = self._run_with_scorer(LowScorer(), min_score=0.5)
        assert report.total_saved == 0
        assert report.total_filtered == 1

    def test_exact_threshold_keeps_image(self):
        """Score == min_score is strictly below threshold → dropped."""

        class ExactScorer:
            def score(self, image_bytes, query):
                return 0.5

        report = self._run_with_scorer(ExactScorer(), min_score=0.5)
        # score 0.5 < min_score 0.5 is False → kept
        # The check is `image_score < job.min_score`, so 0.5 < 0.5 is False
        assert report.total_saved == 1
        assert report.total_filtered == 0

    def test_total_filtered_accumulates(self):
        """total_filtered sums filtered_count across all CategoryResults."""
        from src.downloader import CategoryResult, HarvestReport

        r1 = CategoryResult(folder_slug="a/b", filtered_count=2)
        r2 = CategoryResult(folder_slug="c/d", filtered_count=3)
        report = HarvestReport(categories=[r1, r2])
        assert report.total_filtered == 5
