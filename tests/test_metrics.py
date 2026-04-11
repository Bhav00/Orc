from metrics import MetricsStore


class TestRecordRequest:
    def test_single_request(self, metrics_store):
        metrics_store.record_request("model-a", latency_ms=100.0, prompt_tokens=50, completion_tokens=20)
        d = metrics_store.to_dict()
        m = d["models"]["model-a"]
        assert m["requests"] == 1
        assert m["prompt_tokens"] == 50
        assert m["completion_tokens"] == 20
        assert m["errors"] == 0
        assert m["avg_latency_ms"] == 100.0

    def test_error_increments(self, metrics_store):
        metrics_store.record_request("model-a", latency_ms=50.0, error=True)
        d = metrics_store.to_dict()
        assert d["models"]["model-a"]["errors"] == 1

    def test_multiple_models_tracked_independently(self, metrics_store):
        metrics_store.record_request("model-a", latency_ms=100.0, prompt_tokens=10)
        metrics_store.record_request("model-b", latency_ms=200.0, prompt_tokens=20)
        d = metrics_store.to_dict()
        assert d["models"]["model-a"]["prompt_tokens"] == 10
        assert d["models"]["model-b"]["prompt_tokens"] == 20

    def test_avg_latency_calculation(self, metrics_store):
        metrics_store.record_request("model-a", latency_ms=100.0)
        metrics_store.record_request("model-a", latency_ms=200.0)
        metrics_store.record_request("model-a", latency_ms=300.0)
        d = metrics_store.to_dict()
        assert d["models"]["model-a"]["avg_latency_ms"] == 200.0

    def test_zero_requests_avg_latency(self):
        ms = MetricsStore()
        d = ms.to_dict()
        assert d["models"] == {}


class TestRecordSpawnKill:
    def test_spawn_and_kill(self, metrics_store):
        metrics_store.record_spawn("model-a")
        d = metrics_store.to_dict()
        assert d["process"]["spawns"] == 1
        assert d["process"]["current_model"] == "model-a"
        assert d["process"]["current_model_uptime_s"] is not None

        metrics_store.record_kill()
        d = metrics_store.to_dict()
        assert d["process"]["kills"] == 1
        assert d["process"]["current_model"] is None
        assert d["process"]["current_model_uptime_s"] is None

    def test_multiple_spawns(self, metrics_store):
        metrics_store.record_spawn("a")
        metrics_store.record_kill()
        metrics_store.record_spawn("b")
        d = metrics_store.to_dict()
        assert d["process"]["spawns"] == 2
        assert d["process"]["kills"] == 1
        assert d["process"]["current_model"] == "b"


class TestToPrometheus:
    def test_contains_expected_metrics(self, metrics_store):
        metrics_store.record_request("model-a", latency_ms=100.0, prompt_tokens=50, completion_tokens=20)
        metrics_store.record_spawn("model-a")
        text = metrics_store.to_prometheus()

        assert 'orc_model_requests_total{model="model-a"} 1' in text
        assert 'orc_model_prompt_tokens_total{model="model-a"} 50' in text
        assert 'orc_model_completion_tokens_total{model="model-a"} 20' in text
        assert 'orc_model_errors_total{model="model-a"} 0' in text
        assert "orc_process_spawns_total 1" in text
        assert "orc_process_kills_total 0" in text
        assert "orc_current_model_uptime_seconds" in text

    def test_no_uptime_when_no_model(self, metrics_store):
        text = metrics_store.to_prometheus()
        assert "orc_current_model_uptime_seconds" not in text
