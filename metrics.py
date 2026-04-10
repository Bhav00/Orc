import time
from dataclasses import dataclass, field


@dataclass
class ModelMetrics:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / self.requests, 1) if self.requests else 0.0


class MetricsStore:
    """In-process counters for requests and process-level events.

    All access is from the asyncio event loop — no locking needed.
    Counters reset on orchestrator restart; persistence is not implemented.
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelMetrics] = {}
        self._spawns: int = 0
        self._kills: int = 0
        self._current_model: str | None = None
        self._current_model_loaded_at: float | None = None

    # --- per-request ---

    def record_request(
        self,
        model_id: str,
        latency_ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: bool = False,
    ) -> None:
        m = self._models.setdefault(model_id, ModelMetrics())
        m.requests += 1
        m.total_latency_ms += latency_ms
        m.prompt_tokens += prompt_tokens
        m.completion_tokens += completion_tokens
        if error:
            m.errors += 1

    # --- process-level (called by ProcessManager) ---

    def record_spawn(self, model_id: str) -> None:
        self._spawns += 1
        self._current_model = model_id
        self._current_model_loaded_at = time.monotonic()

    def record_kill(self) -> None:
        self._kills += 1
        self._current_model = None
        self._current_model_loaded_at = None

    # --- serialisation ---

    def to_prometheus(self) -> str:
        """Render counters in Prometheus exposition format (text/plain)."""
        lines: list[str] = []

        lines.append("# HELP orc_model_requests_total Total requests per model.")
        lines.append("# TYPE orc_model_requests_total counter")
        for mid, m in self._models.items():
            lines.append(f'orc_model_requests_total{{model="{mid}"}} {m.requests}')

        lines.append("# HELP orc_model_prompt_tokens_total Total prompt tokens per model.")
        lines.append("# TYPE orc_model_prompt_tokens_total counter")
        for mid, m in self._models.items():
            lines.append(f'orc_model_prompt_tokens_total{{model="{mid}"}} {m.prompt_tokens}')

        lines.append("# HELP orc_model_completion_tokens_total Total completion tokens per model.")
        lines.append("# TYPE orc_model_completion_tokens_total counter")
        for mid, m in self._models.items():
            lines.append(f'orc_model_completion_tokens_total{{model="{mid}"}} {m.completion_tokens}')

        lines.append("# HELP orc_model_errors_total Total errors per model.")
        lines.append("# TYPE orc_model_errors_total counter")
        for mid, m in self._models.items():
            lines.append(f'orc_model_errors_total{{model="{mid}"}} {m.errors}')

        lines.append("# HELP orc_model_avg_latency_ms Average request latency per model.")
        lines.append("# TYPE orc_model_avg_latency_ms gauge")
        for mid, m in self._models.items():
            lines.append(f'orc_model_avg_latency_ms{{model="{mid}"}} {m.avg_latency_ms}')

        lines.append("# HELP orc_process_spawns_total Total model spawns.")
        lines.append("# TYPE orc_process_spawns_total counter")
        lines.append(f"orc_process_spawns_total {self._spawns}")

        lines.append("# HELP orc_process_kills_total Total model kills.")
        lines.append("# TYPE orc_process_kills_total counter")
        lines.append(f"orc_process_kills_total {self._kills}")

        if self._current_model_loaded_at is not None:
            uptime = round(time.monotonic() - self._current_model_loaded_at, 1)
            lines.append("# HELP orc_current_model_uptime_seconds Uptime of the current model.")
            lines.append("# TYPE orc_current_model_uptime_seconds gauge")
            lines.append(f"orc_current_model_uptime_seconds {uptime}")

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def to_dict(self) -> dict:
        uptime = (
            round(time.monotonic() - self._current_model_loaded_at, 1)
            if self._current_model_loaded_at is not None
            else None
        )
        return {
            "models": {
                mid: {
                    "requests": m.requests,
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "errors": m.errors,
                    "avg_latency_ms": m.avg_latency_ms,
                }
                for mid, m in self._models.items()
            },
            "process": {
                "spawns": self._spawns,
                "kills": self._kills,
                "current_model": self._current_model,
                "current_model_uptime_s": uptime,
            },
        }
