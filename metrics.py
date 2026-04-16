import json
import os
import time
from dataclasses import dataclass, field


@dataclass
class ModelMetrics:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0
    empty_responses: int = 0
    finish_reason_counts: dict[str, int] = field(default_factory=dict)

    @property
    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / self.requests, 1) if self.requests else 0.0


class MetricsStore:
    """In-process counters for requests and process-level events.

    All access is from the asyncio event loop — no locking needed.
    Aggregate counters can be saved to / loaded from a JSON snapshot file so
    they survive orchestrator restarts.  Per-request history is stored
    separately in SQLite (see db.py).
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
        finish_reason: str | None = None,
    ) -> None:
        m = self._models.setdefault(model_id, ModelMetrics())
        m.requests += 1
        m.total_latency_ms += latency_ms
        m.prompt_tokens += prompt_tokens
        m.completion_tokens += completion_tokens
        if error:
            m.errors += 1
        if completion_tokens == 0 and not error:
            m.empty_responses += 1
        if finish_reason is not None:
            m.finish_reason_counts[finish_reason] = m.finish_reason_counts.get(finish_reason, 0) + 1

    # --- process-level (called by ProcessManager) ---

    def record_spawn(self, model_id: str) -> None:
        self._spawns += 1
        self._current_model = model_id
        self._current_model_loaded_at = time.monotonic()

    def record_kill(self) -> None:
        self._kills += 1
        self._current_model = None
        self._current_model_loaded_at = None

    # --- snapshot persistence (flat JSON for quick restart recovery) ---

    def save_to_file(self, path: str) -> None:
        """Write aggregate counters to *path* as JSON (atomic replace)."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        data = {
            "models": {
                mid: {
                    "requests": m.requests,
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "errors": m.errors,
                    "total_latency_ms": m.total_latency_ms,
                    "empty_responses": m.empty_responses,
                    "finish_reason_counts": m.finish_reason_counts,
                }
                for mid, m in self._models.items()
            },
            "spawns": self._spawns,
            "kills": self._kills,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)

    def load_from_file(self, path: str) -> None:
        """Restore aggregate counters from a JSON snapshot (silently skips if missing)."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for mid, vals in data.get("models", {}).items():
            m = self._models.setdefault(mid, ModelMetrics())
            m.requests = vals.get("requests", 0)
            m.prompt_tokens = vals.get("prompt_tokens", 0)
            m.completion_tokens = vals.get("completion_tokens", 0)
            m.errors = vals.get("errors", 0)
            m.total_latency_ms = vals.get("total_latency_ms", 0.0)
            m.empty_responses = vals.get("empty_responses", 0)
            m.finish_reason_counts = vals.get("finish_reason_counts", {})
        self._spawns = data.get("spawns", 0)
        self._kills = data.get("kills", 0)

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

        lines.append("# HELP orc_model_empty_responses_total Total empty responses per model.")
        lines.append("# TYPE orc_model_empty_responses_total counter")
        for mid, m in self._models.items():
            lines.append(f'orc_model_empty_responses_total{{model="{mid}"}} {m.empty_responses}')

        lines.append("# HELP orc_model_finish_reason_total Finish reason counts per model.")
        lines.append("# TYPE orc_model_finish_reason_total counter")
        for mid, m in self._models.items():
            for reason, count in m.finish_reason_counts.items():
                lines.append(f'orc_model_finish_reason_total{{model="{mid}",reason="{reason}"}} {count}')

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
                    "empty_responses": m.empty_responses,
                    "finish_reasons": m.finish_reason_counts,
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
