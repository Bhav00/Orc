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
