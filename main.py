import asyncio
import json
import logging
import logging.handlers
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx as _httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings
from db import MetricsDB
from metrics import MetricsStore
from process_manager import OrcError, ProcessManager
from profiles import BackendEntry, load_profiles
from proxy import proxy_chat_completions, proxy_chat_completions_stream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
log = logging.getLogger("orc.main")
req_log = logging.getLogger("orc.requests")


# ---------------------------------------------------------------------------
# File logging setup
# ---------------------------------------------------------------------------

def _setup_file_logging(log_dir: str) -> None:
    """Add rolling file handlers to the root logger and the request logger."""
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
    rotate_kw = dict(when="midnight", backupCount=14, encoding="utf-8")

    # General application log
    fh = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "orc.log"), **rotate_kw
    )
    fh.setFormatter(fmt)
    logging.getLogger().addHandler(fh)

    # Structured per-request log (one JSON object per line)
    rfh = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "requests.jsonl"), **rotate_kw
    )
    rfh.setFormatter(logging.Formatter("%(message)s"))
    req_log.addHandler(rfh)
    req_log.setLevel(logging.INFO)
    req_log.propagate = False  # do not duplicate into orc.log


# ---------------------------------------------------------------------------
# Session middleware
# ---------------------------------------------------------------------------

class SessionMiddleware(BaseHTTPMiddleware):
    """Attach a session ID to every request.

    Uses X-Session-ID from the incoming request if present, otherwise generates
    a random UUID. The ID is echoed back in the response header.
    """

    async def dispatch(self, request: Request, call_next):
        session_id = request.headers.get("x-session-id") or str(uuid.uuid4())
        request.state.session_id = session_id
        response = await call_next(request)
        response.headers["X-Session-ID"] = session_id
        return response


# ---------------------------------------------------------------------------
# Backend router (round-robin for remote backend profiles)
# ---------------------------------------------------------------------------

class BackendRouter:
    """Round-robin picker with background health checking for remote backends."""

    def __init__(self, poll_interval: float = 30.0) -> None:
        self._counters: dict[str, int] = {}
        self._health: dict[str, bool] = {}
        self._poll_interval = poll_interval
        self._poll_task: asyncio.Task | None = None
        self._all_urls: set[str] = set()

    def register_backends(self, profiles) -> None:
        """Scan profiles for remote backend URLs and register them for polling."""
        self._all_urls.clear()
        for profile in profiles.models.values():
            for be in profile.backends:
                url = be.url.rstrip("/")
                self._all_urls.add(url)
                self._health.setdefault(url, True)

    def pick(self, model_id: str, backends: list[BackendEntry]) -> str:
        """Pick a healthy backend round-robin. Falls back to any if all unhealthy."""
        urls = [be.url.rstrip("/") for be in backends]
        healthy = [u for u in urls if self._health.get(u, True)]
        candidates = healthy if healthy else urls

        idx = self._counters.get(model_id, 0)
        url = candidates[idx % len(candidates)]
        self._counters[model_id] = idx + 1
        return url

    def start_polling(self) -> None:
        if self._all_urls and self._poll_interval > 0:
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name="health-poller"
            )

    def stop_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            await self._check_all()

    async def _check_all(self) -> None:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            for url in list(self._all_urls):
                try:
                    resp = await client.get(f"{url}/health")
                    was_healthy = self._health.get(url, True)
                    now_healthy = resp.status_code == 200
                    self._health[url] = now_healthy
                    if was_healthy and not now_healthy:
                        log.warning("Backend %s is now UNHEALTHY (status %d)", url, resp.status_code)
                    elif not was_healthy and now_healthy:
                        log.info("Backend %s recovered — marking healthy", url)
                except Exception:
                    if self._health.get(url, True):
                        log.warning("Backend %s is UNREACHABLE — marking unhealthy", url)
                    self._health[url] = False


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = False
    # All extra fields (temperature, top_p, max_tokens, tools, etc.) are
    # preserved and forwarded as-is to the child.
    model_config = {"extra": "allow"}


class CompletionRequest(BaseModel):
    model: str
    prompt: str | list[str]
    stream: bool = False
    model_config = {"extra": "allow"}


class AdminLoadRequest(BaseModel):
    model: str


class AdminCustomRunRequest(BaseModel):
    model: str
    flags: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

async def _metrics_snapshot_loop(metrics: MetricsStore, path: str, interval: int) -> None:
    """Periodically write the in-memory metrics snapshot to disk."""
    while True:
        await asyncio.sleep(interval)
        try:
            metrics.save_to_file(path)
        except Exception as exc:
            log.warning("Failed to write metrics snapshot: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_file_logging(settings.log_dir)
    log.info("Loading profiles from %s", settings.profiles_path)
    profiles = load_profiles(settings.profiles_path)
    log.info("Loaded %d model profile(s): %s", len(profiles.models), list(profiles.models.keys()))

    # Metrics — restore aggregate counters from the JSON snapshot if it exists
    snapshot_path = os.path.join(settings.log_dir, "metrics.json")
    metrics = MetricsStore()
    if settings.metrics_snapshot_interval > 0:
        metrics.load_from_file(snapshot_path)
        log.info("Metrics snapshot loaded from %s", snapshot_path)

    # SQLite — per-request historical store
    db_path = os.path.join(settings.log_dir, "metrics.db")
    db = MetricsDB(db_path)
    try:
        await db.init()
        log.info("Metrics DB initialised at %s", db_path)
    except Exception as exc:
        log.error("Failed to initialise metrics DB: %s — history endpoint will be unavailable", exc)
        db = None  # type: ignore[assignment]

    pm = ProcessManager(settings)
    pm.set_profiles(profiles)
    pm.set_metrics(metrics)
    pm.start_idle_reaper()

    router = BackendRouter(poll_interval=settings.backend_health_interval)
    router.register_backends(profiles)
    router.start_polling()

    app.state.process_manager = pm
    app.state.profiles = profiles
    app.state.metrics = metrics
    app.state.backend_router = router
    app.state.db = db

    # Start periodic snapshot task
    snapshot_task: asyncio.Task | None = None
    if settings.metrics_snapshot_interval > 0:
        snapshot_task = asyncio.create_task(
            _metrics_snapshot_loop(metrics, snapshot_path, settings.metrics_snapshot_interval),
            name="metrics-snapshot",
        )

    if settings.preload_model:
        model_id = settings.preload_model
        if model_id in profiles.models:
            log.info("Preloading model %r on startup", model_id)
            await pm.ensure_model(model_id)
        else:
            log.warning("PRELOAD_MODEL=%r not found in profiles — skipping preload", model_id)

    yield

    log.info("Shutting down — unloading any running model")
    router.stop_polling()
    pm.stop_idle_reaper()
    await pm.kill_current()

    # Save final snapshot and close DB
    if snapshot_task is not None:
        snapshot_task.cancel()
    if settings.metrics_snapshot_interval > 0:
        try:
            metrics.save_to_file(snapshot_path)
            log.info("Final metrics snapshot saved to %s", snapshot_path)
        except Exception as exc:
            log.warning("Failed to write final metrics snapshot: %s", exc)
    if db is not None:
        await db.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Orc", version="0.7.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware)

if settings.cors_origins:
    origins = [o.strip() for o in settings.cors_origins.split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def require_admin(x_admin_key: str | None = Header(None)) -> None:
    """FastAPI dependency: validates the X-Admin-Key header."""
    if settings.admin_key is None:
        raise HTTPException(status_code=503, detail="Admin key not configured on this server")
    if x_admin_key != settings.admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key header")


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(OrcError)
async def orc_error_handler(request: Request, exc: OrcError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.error_type in ("swap_timeout", "swap_queue_full"):
        # Tell clients how long to wait before retrying (rough heuristic: 5s)
        headers["Retry-After"] = "5"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "code": exc.code,
            },
            "stderr_tail": exc.stderr_tail,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status(request: Request) -> dict:
    pm: ProcessManager = request.app.state.process_manager
    return pm.get_status()


@app.get("/v1/models")
async def list_models(request: Request) -> dict:
    profiles = request.app.state.profiles
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "display_name": profile.display_name,
                "backend_mode": "remote" if profile.backends else "local",
            }
            for model_id, profile in profiles.models.items()
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    pm: ProcessManager = request.app.state.process_manager
    profiles = request.app.state.profiles
    router: BackendRouter = request.app.state.backend_router
    metrics: MetricsStore = request.app.state.metrics
    db: MetricsDB | None = getattr(request.app.state, "db", None)
    session_id: str = getattr(request.state, "session_id", "-")
    t0 = time.monotonic()

    prompt_tokens = completion_tokens = 0
    http_status = 200
    had_error = False

    try:
        profile = profiles.models.get(body.model)
        if profile is None:
            raise OrcError(404, f"Unknown model: {body.model!r}. Check profiles.yaml.")

        if profile.backends:
            # Remote backend mode — no local spawn
            target_url = router.pick(body.model, profile.backends)
            pm_for_proxy = None
        else:
            # Local spawn mode
            await pm.ensure_model(body.model)
            target_url = f"http://127.0.0.1:{settings.child_port}"
            pm_for_proxy = pm

        body_dict = body.model_dump()

        # Merge profile sampling defaults (client-supplied params take precedence)
        if profile.sampling_defaults:
            for key, value in profile.sampling_defaults.items():
                body_dict.setdefault(key, value)

        if body.stream:
            def _on_stream_finish(pt: int, ct: int) -> None:
                latency_ms = (time.monotonic() - t0) * 1000
                metrics.record_request(
                    model_id=body.model,
                    latency_ms=latency_ms,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    error=False,
                )
                req_log.info(
                    json.dumps({
                        "session_id": session_id,
                        "model": body.model,
                        "stream": True,
                        "latency_ms": round(latency_ms, 1),
                        "status": 200,
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                    })
                )
                if db is not None:
                    asyncio.create_task(db.insert_request(
                        session_id=session_id, model=body.model, stream=True,
                        latency_ms=latency_ms, status=200,
                        prompt_tokens=pt, completion_tokens=ct, error=False,
                    ))

            gen = await proxy_chat_completions_stream(
                request_body=body_dict,
                target_url=target_url,
                process_manager=pm_for_proxy,
                on_finish=_on_stream_finish,
            )
            return StreamingResponse(gen, media_type="text/event-stream")

        result = await proxy_chat_completions(
            request_body=body_dict,
            target_url=target_url,
            process_manager=pm_for_proxy,
        )
        usage = result.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        return result

    except OrcError as exc:
        had_error = True
        http_status = exc.status_code
        raise

    finally:
        # Streaming metrics are recorded via the on_finish callback
        if not body.stream:
            latency_ms = (time.monotonic() - t0) * 1000
            metrics.record_request(
                model_id=body.model,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=had_error,
            )
            req_log.info(
                json.dumps({
                    "session_id": session_id,
                    "model": body.model,
                    "stream": False,
                    "latency_ms": round(latency_ms, 1),
                    "status": http_status,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                })
            )
            if db is not None:
                asyncio.create_task(db.insert_request(
                    session_id=session_id, model=body.model, stream=False,
                    latency_ms=latency_ms, status=http_status,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    error=had_error,
                ))


@app.post("/v1/completions")
async def completions(body: CompletionRequest, request: Request):
    pm: ProcessManager = request.app.state.process_manager
    profiles = request.app.state.profiles
    router: BackendRouter = request.app.state.backend_router
    metrics: MetricsStore = request.app.state.metrics
    db: MetricsDB | None = getattr(request.app.state, "db", None)
    session_id: str = getattr(request.state, "session_id", "-")
    t0 = time.monotonic()

    prompt_tokens = completion_tokens = 0
    http_status = 200
    had_error = False

    try:
        profile = profiles.models.get(body.model)
        if profile is None:
            raise OrcError(404, f"Unknown model: {body.model!r}. Check profiles.yaml.")

        if profile.backends:
            target_url = router.pick(body.model, profile.backends)
            pm_for_proxy = None
        else:
            await pm.ensure_model(body.model)
            target_url = f"http://127.0.0.1:{settings.child_port}"
            pm_for_proxy = pm

        body_dict = body.model_dump()

        if profile.sampling_defaults:
            for key, value in profile.sampling_defaults.items():
                body_dict.setdefault(key, value)

        if body.stream:
            def _on_stream_finish(pt: int, ct: int) -> None:
                latency_ms = (time.monotonic() - t0) * 1000
                metrics.record_request(
                    model_id=body.model, latency_ms=latency_ms,
                    prompt_tokens=pt, completion_tokens=ct, error=False,
                )
                req_log.info(json.dumps({
                    "session_id": session_id, "model": body.model,
                    "stream": True, "latency_ms": round(latency_ms, 1),
                    "status": 200, "prompt_tokens": pt, "completion_tokens": ct,
                }))
                if db is not None:
                    asyncio.create_task(db.insert_request(
                        session_id=session_id, model=body.model, stream=True,
                        latency_ms=latency_ms, status=200,
                        prompt_tokens=pt, completion_tokens=ct, error=False,
                    ))

            gen = await proxy_chat_completions_stream(
                request_body=body_dict, target_url=target_url,
                process_manager=pm_for_proxy,
                endpoint_path="/v1/completions",
                on_finish=_on_stream_finish,
            )
            return StreamingResponse(gen, media_type="text/event-stream")

        result = await proxy_chat_completions(
            request_body=body_dict, target_url=target_url,
            process_manager=pm_for_proxy,
            endpoint_path="/v1/completions",
        )
        usage = result.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        return result

    except OrcError as exc:
        had_error = True
        http_status = exc.status_code
        raise

    finally:
        if not body.stream:
            latency_ms = (time.monotonic() - t0) * 1000
            metrics.record_request(
                model_id=body.model, latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                error=had_error,
            )
            req_log.info(json.dumps({
                "session_id": session_id, "model": body.model,
                "stream": False, "latency_ms": round(latency_ms, 1),
                "status": http_status, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }))
            if db is not None:
                asyncio.create_task(db.insert_request(
                    session_id=session_id, model=body.model, stream=False,
                    latency_ms=latency_ms, status=http_status,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    error=had_error,
                ))


@app.get("/metrics")
async def get_metrics(request: Request) -> dict:
    """In-process counters: per-model request stats and process-level spawn/kill counts."""
    metrics: MetricsStore = request.app.state.metrics
    return metrics.to_dict()


@app.get("/metrics/prometheus")
async def get_metrics_prometheus(request: Request) -> PlainTextResponse:
    """Metrics in Prometheus exposition format (text/plain)."""
    metrics: MetricsStore = request.app.state.metrics
    return PlainTextResponse(metrics.to_prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/metrics/history")
async def get_metrics_history(
    request: Request,
    hours: int = 24,
    model: str | None = None,
) -> dict:
    """Per-request history from SQLite, aggregated over the last *hours* hours.

    Query params:
        hours (int): Look-back window in hours (default 24).
        model (str): Restrict to a specific model ID (optional).
    """
    db: MetricsDB | None = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={"error": "SQLite metrics store is not available"},
        )
    return await db.query_history(hours=hours, model=model)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.post("/admin/load")
async def admin_load(
    body: AdminLoadRequest,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Pre-load a model into VRAM. Requires X-Admin-Key header.
    For remote-backend profiles, returns immediately (nothing to spawn).
    """
    profiles = request.app.state.profiles
    profile = profiles.models.get(body.model)
    if profile is None:
        raise OrcError(404, f"Unknown model: {body.model!r}. Check profiles.yaml.")

    if profile.backends:
        return {"status": "ok", "model": body.model, "note": "remote backends — no local spawn needed"}

    pm: ProcessManager = request.app.state.process_manager
    await pm.ensure_model(body.model)
    return {"status": "ok", "model": body.model}


@app.post("/admin/unload")
async def admin_unload(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Unload the currently running model. Requires X-Admin-Key header."""
    pm: ProcessManager = request.app.state.process_manager
    await pm.kill_current()
    return {"status": "ok"}


@app.post("/admin/force-unload")
async def admin_force_unload(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Kill the current model immediately — no graceful shutdown, no post-kill delay.

    Use when the model is stuck mid-generation and /admin/unload is blocking,
    or when VRAM needs to be reclaimed right now without waiting.
    Unlike /admin/unload, this bypasses the spawn lock and skips the VRAM-drain
    delay, so the next spawn may fail if VRAM has not fully released yet.
    Requires X-Admin-Key header.
    """
    pm: ProcessManager = request.app.state.process_manager
    result = await pm.force_kill()
    return {"status": "ok", **result}


@app.post("/admin/reload-profiles")
async def admin_reload_profiles(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Re-read profiles.yaml from disk and swap the live profile set.

    The currently loaded model (if any) keeps running with its original flags.
    New profiles take effect on the next model switch or admin/load call.
    Requires X-Admin-Key header.
    """
    profiles = load_profiles(settings.profiles_path)
    pm: ProcessManager = request.app.state.process_manager
    pm.set_profiles(profiles)
    request.app.state.profiles = profiles
    router: BackendRouter = request.app.state.backend_router
    router.register_backends(profiles)
    log.info("Reloaded %d profile(s): %s", len(profiles.models), list(profiles.models.keys()))
    return {"status": "ok", "profiles": list(profiles.models.keys())}


@app.post("/admin/custom_run")
async def admin_custom_run(
    body: AdminCustomRunRequest,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Spawn a model with flag overrides merged on top of its profile.

    The `flags` dict is merged with the profile's flags; keys in `flags`
    take precedence. The model is always reloaded (even if already running).
    Only valid for local-spawn profiles (not remote backends).
    Requires X-Admin-Key header.
    """
    pm: ProcessManager = request.app.state.process_manager
    await pm.custom_run(body.model, body.flags)
    return {"status": "ok", "model": body.model, "flags_applied": body.flags}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # Do NOT set loop="uvloop" — not available on Windows.
    )
