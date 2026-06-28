"""Tests for src/retry.py — exponential backoff, jitter, rate limiting.

Async coroutines are driven via asyncio.run to avoid a pytest-asyncio dependency,
following the same pattern used by test_downloader.py.
"""


import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.retry import ProviderRateLimiter, RateLimiter, RetryConfig, retry_async


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# RetryConfig.delay_for
# ---------------------------------------------------------------------------


def test_delay_for_attempt_zero_is_zero():
    cfg = RetryConfig(base_delay_s=2.0, jitter=False)
    assert cfg.delay_for(0) == 0.0


def test_delay_increases_exponentially():
    cfg = RetryConfig(base_delay_s=1.0, max_delay_s=1000.0, jitter=False)
    assert cfg.delay_for(1) == pytest.approx(1.0)
    assert cfg.delay_for(2) == pytest.approx(2.0)
    assert cfg.delay_for(3) == pytest.approx(4.0)


def test_delay_capped_at_max():
    cfg = RetryConfig(base_delay_s=1.0, max_delay_s=3.0, jitter=False)
    assert cfg.delay_for(5) == pytest.approx(3.0)


def test_jitter_bounded_within_exponential_ceiling():
    cfg = RetryConfig(base_delay_s=1.0, max_delay_s=100.0, jitter=True)
    for _ in range(50):
        d = cfg.delay_for(3)  # ceiling = base * 2^2 = 4 s
        assert 0.0 <= d <= 4.0


def test_retry_config_validation():
    with pytest.raises(ValueError):
        RetryConfig(max_attempts=0)
    with pytest.raises(ValueError):
        RetryConfig(base_delay_s=-1.0)
    with pytest.raises(ValueError):
        RetryConfig(base_delay_s=5.0, max_delay_s=1.0)


# ---------------------------------------------------------------------------
# retry_async — success / failure / stop
# ---------------------------------------------------------------------------


def test_retry_succeeds_first_attempt():
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    result = run(retry_async(fn, config=RetryConfig(max_attempts=3)))
    assert result == "ok"
    assert len(calls) == 1


def test_retry_succeeds_on_second_attempt():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("transient")
        return "ok"

    async def _run():
        with patch("src.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await retry_async(
                fn,
                config=RetryConfig(max_attempts=3, base_delay_s=1.0, jitter=False),
            )
            return result, mock_sleep.await_count

    result, sleep_count = run(_run())
    assert result == "ok"
    assert len(calls) == 2
    assert sleep_count == 1


def test_retry_stops_at_max_attempts():
    calls = []

    async def fn():
        calls.append(1)
        raise RuntimeError("always fails")

    async def _run():
        with patch("src.retry.asyncio.sleep", new_callable=AsyncMock):
            await retry_async(fn, config=RetryConfig(max_attempts=4))

    with pytest.raises(RuntimeError, match="always fails"):
        run(_run())
    assert len(calls) == 4


def test_non_retryable_exception_propagates_immediately():
    calls = []

    async def fn():
        calls.append(1)
        raise TypeError("not retryable")

    with pytest.raises(TypeError):
        run(retry_async(fn, config=RetryConfig(max_attempts=5), exc_types=(ValueError,)))

    assert len(calls) == 1


def test_retry_after_header_overrides_backoff():
    """Retry-After header value replaces exponential delay."""
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("rate limited")
        return "done"

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def _run():
        with patch("src.retry.asyncio.sleep", side_effect=fake_sleep):
            return await retry_async(
                fn,
                config=RetryConfig(max_attempts=3, base_delay_s=1.0, jitter=False),
                retry_after_header=lambda: 7.0,
            )

    result = run(_run())
    assert result == "done"
    assert slept == [7.0]


def test_retry_after_none_falls_back_to_exponential():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("fail")
        return "ok"

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def _run():
        with patch("src.retry.asyncio.sleep", side_effect=fake_sleep):
            return await retry_async(
                fn,
                config=RetryConfig(max_attempts=3, base_delay_s=2.0, jitter=False),
                retry_after_header=lambda: None,
            )

    run(_run())
    assert slept == [2.0]  # attempt 1 → base * 2^0 = 2.0


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_burst():
    """Burst of tokens should be immediately consumable without meaningful delay."""

    async def _run():
        limiter = RateLimiter(rate=10.0, burst=5.0)
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        return time.monotonic() - start

    elapsed = run(_run())
    assert elapsed < 0.5


def test_rate_limiter_throttles_after_burst():
    """After burst exhausted, next acquire should incur a delay."""

    async def _run():
        limiter = RateLimiter(rate=10.0, burst=2.0)
        await limiter.acquire()
        await limiter.acquire()
        start = time.monotonic()
        await limiter.acquire()
        return time.monotonic() - start

    elapsed = run(_run())
    assert elapsed >= 0.05  # at least half the expected 0.1 s interval (CI slack)


def test_rate_limiter_invalid_rate():
    with pytest.raises(ValueError):
        RateLimiter(rate=0.0)
    with pytest.raises(ValueError):
        RateLimiter(rate=-1.0)


# ---------------------------------------------------------------------------
# ProviderRateLimiter
# ---------------------------------------------------------------------------


def test_provider_rate_limiter_unconstrained_no_wait():
    async def _run():
        prl = ProviderRateLimiter({"pexels": 5.0})
        start = time.monotonic()
        await prl.acquire("duckduckgo")
        return time.monotonic() - start

    elapsed = run(_run())
    assert elapsed < 0.1


def test_provider_rate_limiter_has_limiter():
    prl = ProviderRateLimiter({"unsplash": 2.0})
    assert prl.has_limiter("unsplash")
    assert not prl.has_limiter("pexels")


def test_provider_rate_limiter_empty():
    async def _run():
        prl = ProviderRateLimiter()
        await prl.acquire("anything")  # should not raise

    run(_run())
