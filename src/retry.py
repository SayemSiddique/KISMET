"""Exponential-backoff retry helpers and token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class RetryConfig:
    """Parameters controlling retry behaviour."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay_s: float = 1.0,
        max_delay_s: float = 60.0,
        jitter: bool = True,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if base_delay_s < 0:
            raise ValueError("base_delay_s must be >= 0")
        if max_delay_s < base_delay_s:
            raise ValueError("max_delay_s must be >= base_delay_s")
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter

    def delay_for(self, attempt: int) -> float:
        """Return sleep duration (seconds) before *attempt* (0-indexed).

        attempt=0 → no wait (first try). attempt≥1 → exponential + optional jitter.
        Respects ``max_delay_s`` ceiling.
        """
        if attempt == 0:
            return 0.0
        raw = self.base_delay_s * (2 ** (attempt - 1))
        capped = min(raw, self.max_delay_s)
        if self.jitter:
            return random.uniform(0.0, capped)
        return capped


_DEFAULT_RETRY = RetryConfig()


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    config: RetryConfig | None = None,
    retry_after_header: Callable[[], float | None] | None = None,
    exc_types: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Call *fn* up to ``config.max_attempts`` times, with exponential back-off.

    Parameters
    ----------
    fn:
        Async callable that takes no arguments.
    config:
        Retry configuration; uses module default (3 attempts, 1 s base) if omitted.
    retry_after_header:
        Optional zero-argument callable that returns the number of seconds to wait
        as specified by a ``Retry-After`` response header (or ``None`` if absent).
        When present and non-None, this overrides the exponential calculation.
    exc_types:
        Tuple of exception types that trigger a retry.  Any other exception propagates
        immediately.
    """
    cfg = config or _DEFAULT_RETRY
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(cfg.max_attempts):
        if attempt:
            # Honour Retry-After header if the caller can supply it
            if retry_after_header is not None:
                ra = retry_after_header()
                if ra is not None:
                    await asyncio.sleep(max(0.0, ra))
                else:
                    await asyncio.sleep(cfg.delay_for(attempt))
            else:
                await asyncio.sleep(cfg.delay_for(attempt))
        try:
            return await fn()
        except exc_types as exc:
            last_exc = exc
    raise last_exc


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe (asyncio) token-bucket rate limiter.

    Parameters
    ----------
    rate:
        Maximum sustained queries per second.
    burst:
        Maximum burst size (tokens that can accumulate while idle).  Defaults to
        ``ceil(rate)`` — one full second of capacity.
    """

    def __init__(self, rate: float, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.burst = burst if burst is not None else max(1.0, rate)
        self._tokens: float = self.burst
        self._last: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last = now

    async def acquire(self) -> None:
        """Block until one token is available."""
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Sleep until we'd have a token
            deficit = 1.0 - self._tokens
            wait = deficit / self.rate
            await asyncio.sleep(wait)


class ProviderRateLimiter:
    """Collection of per-provider :class:`RateLimiter` instances.

    Parameters
    ----------
    per_provider_qps:
        Dict mapping provider name → max queries per second.  Providers absent from
        the dict are unconstrained (no limiter created).
    """

    def __init__(self, per_provider_qps: dict[str, float] | None = None) -> None:
        self._limiters: dict[str, RateLimiter] = {}
        if per_provider_qps:
            for provider, qps in per_provider_qps.items():
                self._limiters[provider] = RateLimiter(rate=qps)

    async def acquire(self, provider: str) -> None:
        """Acquire a token for *provider* (no-op if provider is unconstrained)."""
        limiter = self._limiters.get(provider)
        if limiter is not None:
            await limiter.acquire()

    def has_limiter(self, provider: str) -> bool:
        return provider in self._limiters
