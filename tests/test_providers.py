"""Phase 1 + Phase 3 tests: multi-provider discovery, failover, rich metadata, and config.

Offline — every HTTP call is faked with httpx.MockTransport; no network or keys.
"""

import asyncio

import httpx
import pytest

import src.downloader as dl
from src.config import DiscoveryConfig, load_discovery_config
from src.downloader import (
    DiscoveryError,
    DiscoveryResult,
    MultiProvider,
    OpenverseProvider,
    PexelsProvider,
    PixabayProvider,
    UnsplashProvider,
    WikimediaCommonsProvider,
    build_provider,
)


def _run(coro):
    return asyncio.run(coro)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _discover(provider, handler, query="cats", count=2):
    async def run():
        async with _client(handler) as client:
            return await provider.discover(client, query, count)

    return _run(run())


def _discover_rich(provider, handler, query="cats", count=2):
    async def run():
        async with _client(handler) as client:
            return await provider.discover_with_meta(client, query, count)

    return _run(run())


# ---------------------------------------------------------------------------
# Individual JSON providers extract the right URLs from their payload shapes
# ---------------------------------------------------------------------------


class TestJsonProviders:
    def test_openverse(self) -> None:
        def handler(_req):
            return httpx.Response(
                200, json={"results": [{"url": "https://o/1.jpg"}, {"url": "https://o/2.jpg"}]}
            )

        assert _discover(OpenverseProvider(), handler) == ["https://o/1.jpg", "https://o/2.jpg"]

    def test_wikimedia_prefers_thumburl(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": {
                            "1": {
                                "imageinfo": [
                                    {"thumburl": "https://w/t.jpg", "url": "https://w/full.jpg"}
                                ]
                            },
                        }
                    }
                },
            )

        assert _discover(WikimediaCommonsProvider(), handler) == ["https://w/t.jpg"]

    def test_unsplash(self) -> None:
        def handler(_req):
            return httpx.Response(200, json={"results": [{"urls": {"regular": "https://u/1.jpg"}}]})

        assert _discover(UnsplashProvider("key"), handler) == ["https://u/1.jpg"]

    def test_unsplash_sends_client_id_header(self) -> None:
        seen = {}

        def handler(req):
            seen["auth"] = req.headers.get("Authorization")
            return httpx.Response(200, json={"results": [{"urls": {"regular": "https://u/1.jpg"}}]})

        _discover(UnsplashProvider("secret"), handler)
        assert seen["auth"] == "Client-ID secret"

    def test_pexels(self) -> None:
        def handler(_req):
            return httpx.Response(200, json={"photos": [{"src": {"large": "https://p/1.jpg"}}]})

        assert _discover(PexelsProvider("key"), handler) == ["https://p/1.jpg"]

    def test_pixabay(self) -> None:
        def handler(_req):
            return httpx.Response(200, json={"hits": [{"largeImageURL": "https://x/1.jpg"}]})

        assert _discover(PixabayProvider("key"), handler) == ["https://x/1.jpg"]

    def test_empty_results_raise_discovery_error(self) -> None:
        def handler(_req):
            return httpx.Response(200, json={"results": []})

        with pytest.raises(DiscoveryError):
            _discover(OpenverseProvider(), handler)

    def test_http_error_raises_discovery_error(self) -> None:
        def handler(_req):
            return httpx.Response(500, json={})

        with pytest.raises(DiscoveryError):
            _discover(OpenverseProvider(), handler)

    def test_count_caps_results(self) -> None:
        def handler(_req):
            return httpx.Response(
                200, json={"results": [{"url": f"https://o/{i}.jpg"} for i in range(10)]}
            )

        assert len(_discover(OpenverseProvider(), handler, count=3)) == 3


# ---------------------------------------------------------------------------
# Phase 3: discover_with_meta returns DiscoveryResult with license / attribution
# ---------------------------------------------------------------------------


class TestDiscoverWithMeta:
    def test_openverse_extracts_license_and_creator(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://o/1.jpg",
                            "license": "cc-by",
                            "license_version": "4.0",
                            "creator": "Alice",
                            "attribution": "Photo by Alice (CC BY 4.0)",
                        }
                    ]
                },
            )

        results = _discover_rich(OpenverseProvider(), handler)
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, DiscoveryResult)
        assert r.url == "https://o/1.jpg"
        assert r.license == "cc-by 4.0"
        assert r.author == "Alice"
        assert r.attribution == "Photo by Alice (CC BY 4.0)"
        assert r.provider == "openverse"

    def test_wikimedia_extracts_license_from_extmetadata(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": {
                            "1": {
                                "imageinfo": [
                                    {
                                        "thumburl": "https://w/t.jpg",
                                        "extmetadata": {
                                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                            "Artist": {"value": "Bob"},
                                        },
                                    }
                                ]
                            },
                        }
                    }
                },
            )

        results = _discover_rich(WikimediaCommonsProvider(), handler)
        assert results[0].license == "CC BY-SA 4.0"
        assert results[0].author == "Bob"

    def test_wikimedia_handles_missing_extmetadata(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": {
                            "1": {
                                "imageinfo": [
                                    {"thumburl": "https://w/t.jpg", "url": "https://w/full.jpg"}
                                ]
                            },
                        }
                    }
                },
            )

        results = _discover_rich(WikimediaCommonsProvider(), handler)
        assert results[0].url == "https://w/t.jpg"
        assert results[0].license == ""

    def test_wikimedia_strips_html_from_artist(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": {
                            "1": {
                                "imageinfo": [
                                    {
                                        "thumburl": "https://w/t.jpg",
                                        "extmetadata": {
                                            "LicenseShortName": {"value": "PD"},
                                            "Artist": {"value": "<span>Carol</span>"},
                                        },
                                    }
                                ]
                            },
                        }
                    }
                },
            )

        results = _discover_rich(WikimediaCommonsProvider(), handler)
        assert results[0].author == "Carol"

    def test_unsplash_builds_attribution(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "urls": {"regular": "https://u/1.jpg"},
                            "user": {"name": "Dave"},
                            "links": {"html": "https://unsplash.com/@dave"},
                        }
                    ]
                },
            )

        results = _discover_rich(UnsplashProvider("key"), handler)
        r = results[0]
        assert r.license == "Unsplash License"
        assert r.author == "Dave"
        assert "Dave" in r.attribution
        assert "Unsplash" in r.attribution

    def test_pexels_builds_attribution(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "photos": [
                        {
                            "src": {"large": "https://p/1.jpg"},
                            "photographer": "Eve",
                            "url": "https://pexels.com/photo/1",
                        }
                    ]
                },
            )

        results = _discover_rich(PexelsProvider("key"), handler)
        r = results[0]
        assert r.license == "Pexels License"
        assert r.author == "Eve"
        assert "Pexels" in r.attribution

    def test_pixabay_builds_attribution(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "largeImageURL": "https://x/1.jpg",
                            "user": "Frank",
                            "pageURL": "https://pixabay.com/photos/1",
                        }
                    ]
                },
            )

        results = _discover_rich(PixabayProvider("key"), handler)
        r = results[0]
        assert r.license == "Pixabay License"
        assert r.author == "Frank"

    def test_discover_plain_still_returns_urls(self) -> None:
        """discover() must still work (Protocol compat) after _extract_results refactor."""

        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://o/1.jpg"},
                        {"url": "https://o/2.jpg"},
                    ]
                },
            )

        assert _discover(OpenverseProvider(), handler) == ["https://o/1.jpg", "https://o/2.jpg"]


# ---------------------------------------------------------------------------
# MultiProvider failover
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, result=None, fail=False):
        self.result = result or []
        self.fail = fail
        self.calls = 0

    async def discover(self, client, query, count, image_type_filter=""):
        self.calls += 1
        if self.fail:
            raise DiscoveryError("stub down")
        return self.result


class TestMultiProvider:
    def test_first_success_short_circuits(self) -> None:
        a = _StubProvider(result=["https://a/1.jpg"])
        b = _StubProvider(result=["https://b/1.jpg"])
        out = _run(MultiProvider([a, b]).discover(None, "q", 1))
        assert out == ["https://a/1.jpg"]
        assert b.calls == 0

    def test_falls_over_to_next_on_error(self) -> None:
        a = _StubProvider(fail=True)
        b = _StubProvider(result=["https://b/1.jpg"])
        out = _run(MultiProvider([a, b]).discover(None, "q", 1))
        assert out == ["https://b/1.jpg"]
        assert a.calls == 1 and b.calls == 1

    def test_raises_when_all_fail(self) -> None:
        providers = [_StubProvider(fail=True), _StubProvider(fail=True)]
        with pytest.raises(DiscoveryError, match="All discovery providers failed"):
            _run(MultiProvider(providers).discover(None, "q", 1))

    def test_requires_at_least_one_provider(self) -> None:
        with pytest.raises(ValueError):
            MultiProvider([])


# ---------------------------------------------------------------------------
# build_provider factory + config resolution
# ---------------------------------------------------------------------------


class TestBuildProvider:
    def test_keyless_only_when_no_keys(self) -> None:
        cfg = DiscoveryConfig(order=["duckduckgo", "openverse", "wikimedia"], api_keys={})
        mp = build_provider(cfg)
        assert isinstance(mp, MultiProvider)
        assert [type(p) for p in mp._providers] == [
            dl.DuckDuckGoProvider,
            OpenverseProvider,
            WikimediaCommonsProvider,
        ]

    def test_keyed_provider_skipped_without_key(self) -> None:
        cfg = DiscoveryConfig(order=["unsplash", "openverse"], api_keys={})
        mp = build_provider(cfg)
        assert [type(p) for p in mp._providers] == [OpenverseProvider]

    def test_keyed_provider_included_with_key(self) -> None:
        cfg = DiscoveryConfig(order=["unsplash"], api_keys={"unsplash": "k"})
        mp = build_provider(cfg)
        assert [type(p) for p in mp._providers] == [UnsplashProvider]

    def test_always_yields_at_least_duckduckgo(self) -> None:
        cfg = DiscoveryConfig(order=["unsplash"], api_keys={})  # keyed, no key
        mp = build_provider(cfg)
        assert [type(p) for p in mp._providers] == [dl.DuckDuckGoProvider]


class TestLoadDiscoveryConfig:
    def test_defaults_with_empty_env(self) -> None:
        cfg = load_discovery_config(env={})
        assert cfg.order[0] == "duckduckgo"
        assert cfg.api_keys == {}

    def test_reads_keys_from_env(self) -> None:
        cfg = load_discovery_config(env={"PEXELS_API_KEY": "pk", "PIXABAY_API_KEY": "xk"})
        assert cfg.key_for("pexels") == "pk"
        assert cfg.key_for("pixabay") == "xk"
        assert cfg.key_for("unsplash") is None

    def test_custom_order_filters_unknown(self) -> None:
        cfg = load_discovery_config(env={"KISMET_PROVIDER_ORDER": "openverse, bogus, pexels"})
        assert cfg.order == ["openverse", "pexels"]

    def test_invalid_order_falls_back_to_default(self) -> None:
        cfg = load_discovery_config(env={"KISMET_PROVIDER_ORDER": "bogus, nonsense"})
        assert cfg.order[0] == "duckduckgo"
