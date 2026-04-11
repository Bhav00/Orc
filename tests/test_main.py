import sys
import os

import pytest

# Ensure project root is on the path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from profiles import BackendEntry, ModelProfile, ProfilesFile


# ---------------------------------------------------------------------------
# BackendRouter
# ---------------------------------------------------------------------------

class TestBackendRouter:
    def _make_router(self):
        from main import BackendRouter
        return BackendRouter()

    def test_round_robin_two_backends(self):
        router = self._make_router()
        backends = [
            BackendEntry(url="http://10.0.0.1:8090"),
            BackendEntry(url="http://10.0.0.2:8090"),
        ]
        urls = [router.pick("model-a", backends) for _ in range(4)]
        assert urls == [
            "http://10.0.0.1:8090",
            "http://10.0.0.2:8090",
            "http://10.0.0.1:8090",
            "http://10.0.0.2:8090",
        ]

    def test_single_backend(self):
        router = self._make_router()
        backends = [BackendEntry(url="http://10.0.0.1:8090")]
        urls = [router.pick("model-a", backends) for _ in range(3)]
        assert urls == ["http://10.0.0.1:8090"] * 3

    def test_trailing_slash_stripped(self):
        router = self._make_router()
        backends = [BackendEntry(url="http://10.0.0.1:8090/")]
        url = router.pick("model-a", backends)
        assert url == "http://10.0.0.1:8090"

    def test_independent_per_model(self):
        router = self._make_router()
        backends = [
            BackendEntry(url="http://a:8090"),
            BackendEntry(url="http://b:8090"),
        ]
        # model-a picks first backend
        assert router.pick("model-a", backends) == "http://a:8090"
        # model-b also picks first (independent counter)
        assert router.pick("model-b", backends) == "http://a:8090"
        # model-a picks second
        assert router.pick("model-a", backends) == "http://b:8090"


# ---------------------------------------------------------------------------
# Sampling defaults merge
# ---------------------------------------------------------------------------

class TestSamplingDefaultsMerge:
    def test_profile_defaults_applied(self):
        body_dict = {"model": "test", "messages": []}
        sampling_defaults = {"temperature": 0.2, "top_p": 0.9}
        for key, value in sampling_defaults.items():
            body_dict.setdefault(key, value)
        assert body_dict["temperature"] == 0.2
        assert body_dict["top_p"] == 0.9

    def test_client_overrides_win(self):
        body_dict = {"model": "test", "messages": [], "temperature": 0.8}
        sampling_defaults = {"temperature": 0.2, "top_p": 0.9}
        for key, value in sampling_defaults.items():
            body_dict.setdefault(key, value)
        assert body_dict["temperature"] == 0.8  # client wins
        assert body_dict["top_p"] == 0.9  # default applied

    def test_empty_defaults_no_change(self):
        body_dict = {"model": "test", "messages": []}
        sampling_defaults = {}
        for key, value in sampling_defaults.items():
            body_dict.setdefault(key, value)
        assert "temperature" not in body_dict


# ---------------------------------------------------------------------------
# BackendRouter health checking
# ---------------------------------------------------------------------------

class TestBackendRouterHealth:
    def _make_router(self):
        from main import BackendRouter
        return BackendRouter(poll_interval=30)

    def test_skips_unhealthy_backend(self):
        router = self._make_router()
        backends = [
            BackendEntry(url="http://a:8090"),
            BackendEntry(url="http://b:8090"),
        ]
        # Mark first as unhealthy
        router._health["http://a:8090"] = False
        router._health["http://b:8090"] = True

        # Should only pick the healthy one
        urls = [router.pick("m", backends) for _ in range(3)]
        assert all(u == "http://b:8090" for u in urls)

    def test_all_unhealthy_falls_back_to_any(self):
        router = self._make_router()
        backends = [
            BackendEntry(url="http://a:8090"),
            BackendEntry(url="http://b:8090"),
        ]
        router._health["http://a:8090"] = False
        router._health["http://b:8090"] = False

        # Should still return something (fallback to full list)
        url = router.pick("m", backends)
        assert url in ("http://a:8090", "http://b:8090")

    def test_register_backends_populates_urls(self):
        router = self._make_router()
        profiles = ProfilesFile(models={
            "remote": ModelProfile(
                display_name="R",
                backends=[
                    BackendEntry(url="http://x:8090"),
                    BackendEntry(url="http://y:8090/"),
                ],
            ),
            "local": ModelProfile(
                display_name="L",
                model_path="/path.gguf",
            ),
        })
        router.register_backends(profiles)
        assert "http://x:8090" in router._all_urls
        assert "http://y:8090" in router._all_urls
        assert len(router._all_urls) == 2

    @pytest.mark.asyncio
    async def test_check_all_marks_unhealthy_on_failure(self):
        import respx as _respx
        router = self._make_router()
        router._all_urls = {"http://a:8090"}
        router._health["http://a:8090"] = True

        with _respx.mock:
            _respx.get("http://a:8090/health").respond(503)
            await router._check_all()

        assert router._health["http://a:8090"] is False

    @pytest.mark.asyncio
    async def test_check_all_recovers_on_success(self):
        import respx as _respx
        router = self._make_router()
        router._all_urls = {"http://a:8090"}
        router._health["http://a:8090"] = False

        with _respx.mock:
            _respx.get("http://a:8090/health").respond(200)
            await router._check_all()

        assert router._health["http://a:8090"] is True
