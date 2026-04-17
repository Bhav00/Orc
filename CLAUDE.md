# CLAUDE.md — Session context for Orc

> **Mandatory rule:** At the end of every session, update both `README.md` and `CLAUDE.md` to reflect any changes made (new endpoints, changed behavior, added files, config changes, phase completions, known issues discovered).

---

## What this project is

Orc is a thin FastAPI orchestrator that wraps `llama-server.exe` (llama.cpp). It:
- Exposes a single OpenAI-compatible API endpoint for all downstream apps
- Manages one `llama-server.exe` child process at a time (swap on model change)
- Captures child stderr and returns it in structured error responses (the core value prop)
- Optionally routes to remote llama-server backends (round-robin, no local spawn)

Target: Windows, single NVIDIA GPU, llama.cpp prebuilt CUDA binaries.

---

## Current phase: Phase 8 complete (LLM output quality safeguards)

**Implemented (Phase 1):**
- Profile loader (`profiles.py`) — YAML → Pydantic models, CLI-arg builder
- Process manager (`process_manager.py`) — spawn/kill state machine, stderr capture
- Non-streaming proxy (`proxy.py`) — error classification, httpx forwarding
- FastAPI app (`main.py`) — routes, lifespan, OrcError exception handler
- Config (`config.py`) — pydantic-settings, all env vars

**Implemented (Phase 2):**
- Streaming proxy (`proxy.py`) — raw SSE passthrough via `proxy_chat_completions_stream`; connection + status-code check happens before first yield so `OrcError` can still be raised cleanly
- Idle reaper (`process_manager.py`) — `start_idle_reaper` / `stop_idle_reaper`, polls every 30 s, evicts after `IDLE_TTL_SECONDS` (disabled when TTL ≤ 0)
- `/admin/load` and `/admin/unload` (`main.py`) — `X-Admin-Key` header auth via `require_admin` dependency

**Implemented (Phase 3 + future iterations):**
- `/admin/custom_run` — kills current, merges caller-supplied flags on top of profile flags, respawns; local-spawn profiles only
- Session-scoped logging — `SessionMiddleware` reads/generates `X-Session-ID`; rolling files via `TimedRotatingFileHandler` (`logs/orc.log` and `logs/requests.jsonl`); per-request JSON log line in `chat_completions` handler
- Usage metrics — `MetricsStore` (`metrics.py`); per-model request/token/error/latency counters; process-level spawn/kill counts; `GET /metrics` endpoint
- Multi-backend routing — `backends: [{url: ...}]` in profile skips local spawn and routes to remote URLs round-robin via `BackendRouter`; `proxy.py` refactored to take `target_url: str` and optional `ProcessManager`

**Implemented (Phase 4 — quality-of-life):**
- Profile hot-reload — `POST /admin/reload-profiles` re-reads `profiles.yaml` from disk; running model keeps its flags until next switch
- Sampling defaults auto-merge — profile `sampling_defaults` are merged into each request body; client-supplied params take precedence via `setdefault`
- CORS middleware — optional `ORCHESTRATOR_CORS_ORIGINS` env var; when set, adds `CORSMiddleware` with specified origins
- Startup preloading — `ORCHESTRATOR_PRELOAD_MODEL` env var; when set, `ensure_model()` is called during lifespan startup
- Prometheus metrics — `GET /metrics/prometheus` returns counters in Prometheus exposition format; `MetricsStore.to_prometheus()` renders text/plain output

**Implemented (Phase 5 — medium improvements):**
- Test suite (`tests/`) — pytest + pytest-asyncio + respx; 56 tests covering profiles, proxy, metrics, backend router, sampling defaults
- Streaming token extraction — `on_finish` callback in `proxy_chat_completions_stream()` parses final SSE `data:` chunk for `usage`; metrics and request log now capture token counts for streaming requests
- `/v1/completions` endpoint — prompt-based text completions; `CompletionRequest` model in `main.py`; `endpoint_path` parameter added to both proxy functions
- Backend health checking — `BackendRouter` expanded with `_health` dict, periodic `/health` polling via `_poll_loop()`, `pick()` filters unhealthy backends (falls back to full list if all down); configurable via `BACKEND_HEALTH_INTERVAL`

**Implemented (Phase 6 — reliability + persistence):**
- Mid-stream error surfacing — `proxy.py` `_gen()` now catches `httpx.ReadError`/`RemoteProtocolError` mid-stream and injects a `data: {"error": ...}` SSE sentinel before closing, so clients can detect connection loss
- Metrics JSON snapshot — `metrics.py`: `save_to_file()` / `load_from_file()` (atomic rename); `main.py`: `_metrics_snapshot_loop()` background task + load on startup + save on shutdown; controlled by `ORCHESTRATOR_METRICS_SNAPSHOT_INTERVAL` (default 60 s; 0 = disabled); path = `{LOG_DIR}/metrics.json`
- Per-request SQLite history — `db.py` (new): `MetricsDB` class backed by `aiosqlite`; `insert_request()` called fire-and-forget (`asyncio.create_task`) from both streaming (`_on_stream_finish`) and non-streaming (`finally`) paths in `chat_completions` and `completions`; path = `{LOG_DIR}/metrics.db`
- `GET /metrics/history` — queries SQLite; `?hours=N&model=id` params; returns per-model aggregates for the look-back window
- `config.py`: added `metrics_snapshot_interval`; `requirements.txt`: added `aiosqlite==0.20.0`; `.env.example`: added `ORCHESTRATOR_METRICS_SNAPSHOT_INTERVAL`
- Version bumped to 0.6.0

**Implemented (Phase 7 — request queuing + force-unload):**
- Request queuing during model swap — `ensure_model()` refactored: `_ensure_model_locked()` extracts the locked body; outer method adds queue-depth guard (`swap_queue_depth > 0` rejects excess waiters immediately) and `asyncio.wait_for()` timeout (`swap_timeout_seconds > 0`, default 30 s); both error types (`swap_timeout`, `swap_queue_full`) return 503 with `Retry-After: 5` header via updated `orc_error_handler`; `_queue_waiters` counter exposed in `GET /status` as `swap_queue_depth`
- Force-unload — `ProcessManager.force_kill()`: sends SIGKILL immediately, cancels stderr reader, resets state to IDLE without acquiring the lock or waiting for `post_kill_delay`; process is reaped in background via `_reap_process()` task; `POST /admin/force-unload` endpoint wired in `main.py`
- `config.py`: added `swap_timeout_seconds: int = Field(30)` and `swap_queue_depth: int = Field(0)`
- `.env.example`: added `ORCHESTRATOR_SWAP_TIMEOUT_SECONDS` and `ORCHESTRATOR_SWAP_QUEUE_DEPTH`
- Version bumped to 0.7.0

**Implemented (Phase 8 — LLM output quality safeguards):**
- Sampling defaults — `profiles.yaml.example`: added `repeat_penalty: 1.1` and `max_tokens: 2048` to all profiles' `sampling_defaults`; merged into every request via existing `setdefault()` logic (client values always win)
- finish_reason extraction — `proxy.py`: extracted from non-streaming responses and from the last SSE chunk in streaming; `on_finish` callback expanded to 3 args `(prompt_tokens, completion_tokens, finish_reason)`; logged in `requests.jsonl`, stored in SQLite, tracked in `MetricsStore`
- Empty response detection — `proxy.py`: logs warning when `completion_tokens == 0` on HTTP 200; `main.py`: non-streaming responses with zero tokens get `X-Orc-Warning: empty-response` header; `finish_reason == "length"` gets `X-Orc-Warning: generation-truncated` header
- Repetition detection — `proxy.py`: `detect_repetition()` function (sliding-window character-level pattern check); streaming `_gen()` runs detector every 20 chunks when `repeat_detection_window > 0`; `abort` mode injects `data: {"error": {..., "type": "repetition_detected"}}` SSE sentinel; `warn` mode logs only; non-streaming path checks post-hoc and adds `X-Orc-Warning: repetition-detected` header
- `metrics.py`: added `empty_responses` and `finish_reason_counts` to `ModelMetrics`; updated `record_request()`, `to_dict()`, `to_prometheus()`, `save_to_file()`, `load_from_file()`
- `db.py`: added `finish_reason TEXT` column to schema; `ALTER TABLE` migration for existing DBs; updated `insert_request()` and `query_history()` (includes finish_reason distribution)
- `config.py`: added `repeat_detection_window`, `repeat_detection_threshold`, `repeat_detection_action`
- `.env.example`: added `ORCHESTRATOR_REPEAT_DETECTION_WINDOW`, `ORCHESTRATOR_REPEAT_DETECTION_THRESHOLD`, `ORCHESTRATOR_REPEAT_DETECTION_ACTION`
- Tests: 73 tests total (up from 56); new tests for `detect_repetition`, streaming repetition abort/warn/disabled, finish_reason extraction, empty response tracking, finish_reason metrics, snapshot round-trip
- Version bumped to 0.8.0

---

## File map

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, routes, `SessionMiddleware`, `BackendRouter`, lifespan, `OrcError` exception handler |
| `config.py` | `Settings` (pydantic-settings), module-level `settings` singleton |
| `metrics.py` | `MetricsStore` — in-process per-model and process-level counters; JSON snapshot save/load |
| `db.py` | `MetricsDB` — `aiosqlite`-backed per-request row store; `query_history()` |
| `profiles.py` | `ModelProfile`, `BackendEntry`, `ProfilesFile`, `load_profiles()`, `build_cli_args()` |
| `process_manager.py` | `ChildState` enum, `OrcError` exception, `ChildInfo` dataclass, `ProcessManager` class |
| `proxy.py` | `classify_stderr()`, `proxy_chat_completions()`, `proxy_chat_completions_stream()` |
| `profiles.yaml.example` | Template profile file — copy to `profiles.yaml` |
| `.env.example` | All env vars with defaults — copy to `.env` |
| `requirements.txt` | Python deps |
| `tests/` | Test suite — `test_profiles.py`, `test_proxy.py`, `test_metrics.py`, `test_main.py`, `conftest.py` |

---

## Key invariants — do not break these

1. **One model at a time.** `asyncio.Lock` in `ProcessManager.ensure_model()` serializes all spawn/kill operations. Never bypass it.
2. **Stderr reader starts before health polling.** `_read_stderr_loop` task is created in `_spawn()` before `_health_poll()` runs, so loading messages are captured even if the child crashes during startup.
3. **`is True / is False` in `build_cli_args`.** Uses identity checks, not truthiness — `0` is a valid flag value and must not be silently dropped.
4. **No `creationflags` on Windows subprocess.** Adding `CREATE_NEW_PROCESS_GROUP` severs the stderr pipe on Windows. Do not add it.
5. **`post_kill_delay` runs while the lock is held.** The sleep in `_kill_and_wait()` is intentional — it blocks new spawns until VRAM is expected to have drained on Windows.
6. **Fail fast on bad profiles YAML.** `load_profiles()` is called at startup; a malformed file crashes the server before it accepts connections.
7. **`OrcError` is defined in `process_manager.py`**, imported by `proxy.py` and `main.py`. Do not move it without updating imports.
8. **`proxy.py` functions take `target_url: str`**, not a port number. The URL is assembled by the caller (`main.py`). `process_manager` parameter is `None` for remote backends.
9. **`custom_run` always kills first.** It never takes the fast path — even if the same model is loaded — because the flags may differ.

---

## How to run locally (on Windows)

```
# 1. Install deps
pip install -r requirements.txt

# 2. Copy and edit configs
copy profiles.yaml.example profiles.yaml
copy .env.example .env
# Edit .env: set ORCHESTRATOR_LLAMA_SERVER_BIN to full path of llama-server.exe
# Edit profiles.yaml: set model_path values to your .gguf files

# 3. Start
python main.py
```

Server listens on `http://127.0.0.1:8080` by default.

---

## Env vars quick reference

All prefixed `ORCHESTRATOR_`. See `.env.example` for full list.

- `LLAMA_SERVER_BIN` — path to `llama-server.exe`
- `CHILD_PORT` — internal port for child (default 8090)
- `PORT` — orchestrator port (default 8080)
- `SPAWN_TIMEOUT_SECONDS` — max wait for health check (default 60)
- `POST_KILL_DELAY_SECONDS` — VRAM drain wait after kill (default 2.0)
- `VRAM_TOTAL_MB` / `VRAM_RESERVE_MB` — sanity check limits
- `IDLE_TTL_SECONDS` — idle eviction timeout; 0 = disabled (default 600)
- `ADMIN_KEY` — required for `/admin/*` routes
- `CORS_ORIGINS` — comma-separated allowed origins, or `*`; empty = disabled
- `PRELOAD_MODEL` — model ID to pre-load on startup; empty = no preload
- `BACKEND_HEALTH_INTERVAL` — health poll interval for remote backends in seconds; 0 = disabled (default 30)
- `LOG_DIR` — directory for rolling log files, JSON snapshot, and SQLite DB (default `logs`)
- `METRICS_SNAPSHOT_INTERVAL` — seconds between JSON snapshot saves; 0 = disabled (default 60)
- `SWAP_TIMEOUT_SECONDS` — max wait for model swap lock before returning 503; 0 = unlimited (default 30)
- `SWAP_QUEUE_DEPTH` — max requests queued for a swap; 0 = unlimited (default 0)

---

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/healthz` | Always `{"status": "ok"}` |
| GET | `/status` | State machine status + PID |
| GET | `/v1/models` | Lists profiles from YAML; includes `backend_mode` field |
| POST | `/v1/chat/completions` | Streaming and non-streaming; merges profile `sampling_defaults` |
| POST | `/v1/completions` | Prompt-based text completions |
| GET | `/metrics` | Per-model + process-level counters (JSON) |
| GET | `/metrics/prometheus` | Same counters in Prometheus exposition format |
| GET | `/metrics/history` | SQLite per-request history; `?hours=N&model=id` |
| POST | `/admin/load` | Pre-load model; requires `X-Admin-Key` |
| POST | `/admin/unload` | Graceful unload (lock + VRAM-drain delay); requires `X-Admin-Key` |
| POST | `/admin/force-unload` | Immediate SIGKILL, no lock, no delay; requires `X-Admin-Key` |
| POST | `/admin/reload-profiles` | Re-read `profiles.yaml` from disk; requires `X-Admin-Key` |
| POST | `/admin/custom_run` | Spawn with flag overrides; requires `X-Admin-Key` |

---

## Error format

```json
{
  "error": { "message": "...", "type": "...", "code": "..." },
  "stderr_tail": ["last", "N", "stderr", "lines"]
}
```

---

## Development branch

`claude/fix-llm-output-issues-M25Gf`

---

## Session log

### 2026-04-08 (session 1)
- Initial implementation: Phase 1 MVP complete
- Created: `main.py`, `config.py`, `profiles.py`, `process_manager.py`, `proxy.py`
- Created: `requirements.txt`, `.env.example`, `profiles.yaml.example`, `README.md`, `CLAUDE.md`
- Added "Future / next iterations" section to README covering: session-scoped logging + rolling log files, usage metrics (`/metrics` endpoint), multi-backend / load balancing layer with per-profile backend URL lists

### 2026-04-08 (session 2)
- Phase 2 complete
- `proxy.py`: added `proxy_chat_completions_stream()` — connection + header check before first yield, SSE passthrough via nested `_gen()` async generator
- `process_manager.py`: added `_last_used_at` tracking in `ensure_model()`, `start_idle_reaper()` / `stop_idle_reaper()`, `_idle_reaper_loop()` (30 s poll, evicts after `IDLE_TTL_SECONDS`)
- `main.py`: `stream` field on `ChatCompletionRequest`, streaming branch in `chat_completions` route, `require_admin` dependency, `/admin/load` and `/admin/unload` endpoints, reaper lifecycle in lifespan
- Updated README and CLAUDE.md to reflect Phase 2 complete

### 2026-04-09 (session 3)
- Phase 3 + future iterations complete
- `metrics.py` (new): `MetricsStore` with per-model request/token/latency counters and process-level spawn/kill tracking
- `profiles.py`: added `BackendEntry`, made `model_path`/`estimated_vram_mb`/`flags` optional, added `backends: list[BackendEntry]`, added `model_validator` enforcing model_path OR backends
- `process_manager.py`: added `set_metrics()`, wired `record_spawn()` / `record_kill()` into `_spawn()` / `_kill_and_wait()`; added `custom_run()` (kills current, merges flags, respawns)
- `proxy.py`: refactored `child_port: int` → `target_url: str`; `process_manager` is now `Optional`; added `_stderr()` helper; logging now shows `target_url`
- `main.py`: added `SessionMiddleware`, `BackendRouter`, `MetricsStore` wiring; `chat_completions` now routes local vs remote, records metrics, logs JSON request line; added `/metrics`, `/admin/custom_run` endpoints; updated `/admin/load` for remote-backend profiles; version bumped to 0.3.0
- `config.py`: added `log_dir` setting
- `.env.example`: added `ORCHESTRATOR_LOG_DIR`
- `profiles.yaml.example`: updated comments + remote-backend example
- Updated README (full rewrite) and CLAUDE.md

### 2026-04-10 (session 4)
- Full audit of README.md and CLAUDE.md against all 6 source files — verified endpoints, env vars, invariants, error format, metrics output, logging, and known limitations all accurate
- Fixed stale development branch reference in CLAUDE.md (`claude/complete-readme-phases-zR4aB` → `claude/review-docs-improvements-vNmMT`)
- Fixed README.md setup section: added Windows `copy` commands alongside Unix `cp`
- Expanded README.md error classification table: split into "Stderr-classified errors" and "Orchestrator/proxy errors", added 6 missing error types (`child_unreachable`, `child_timeout`, `child_connection_error`, `child_error`, `insufficient_vram`, `unsupported_operation`)
- Implemented 5 quality-of-life improvements (Phase 4):
  - `POST /admin/reload-profiles` — hot-reload profiles.yaml without restart
  - Sampling defaults auto-merge — profile `sampling_defaults` injected into requests (client overrides)
  - CORS middleware — `ORCHESTRATOR_CORS_ORIGINS` env var enables browser-based clients
  - Startup preloading — `ORCHESTRATOR_PRELOAD_MODEL` env var warms model on boot
  - `GET /metrics/prometheus` — Prometheus exposition format endpoint
- `config.py`: added `cors_origins` and `preload_model` settings
- `metrics.py`: added `to_prometheus()` method
- `.env.example`: added `ORCHESTRATOR_CORS_ORIGINS` and `ORCHESTRATOR_PRELOAD_MODEL`
- Version bumped to 0.4.0
- Updated README and CLAUDE.md: new endpoints, env vars, removed resolved known limitations

### 2026-04-11 (session 5)
- Phase 5 complete — all medium improvements implemented
- **M1: Test suite** — created `tests/` with `conftest.py`, `test_profiles.py`, `test_proxy.py`, `test_metrics.py`, `test_main.py` (56 tests total); added `pytest`, `pytest-asyncio`, `respx` to `requirements.txt`
- **M3: Streaming token extraction** — `proxy.py`: `on_finish` callback in `proxy_chat_completions_stream()` parses final SSE `data:` line for `usage` in `_gen()` finally block; `main.py`: streaming branch defines `_on_stream_finish` callback, `finally` block guarded with `if not body.stream`
- **M5: `/v1/completions` endpoint** — `proxy.py`: added `endpoint_path` param to both proxy functions; `main.py`: added `CompletionRequest` model and `POST /v1/completions` route
- **M2: Backend health checking** — `main.py`: `BackendRouter` expanded with `_health` dict, `register_backends()`, `start_polling()`/`stop_polling()`, `_poll_loop()`/`_check_all()`; `pick()` filters unhealthy, falls back if all down; `config.py`: added `backend_health_interval`; `.env.example`: added `ORCHESTRATOR_BACKEND_HEALTH_INTERVAL`
- Version bumped to 0.5.0
- Updated README and CLAUDE.md: new endpoints, env vars, removed 2 known limitations (streaming tokens, health checking), added test suite and future plans sections

### 2026-04-11 (session 6)
- Phase 6 complete — reliability + persistence
- **Streaming mid-stream errors** — `proxy.py` `_gen()` except block now captures `_stderr()`, logs, then `yield`s a `data: {"error": ...}` SSE sentinel before the `finally` cleanup so clients can detect mid-stream child death
- **Metrics JSON snapshot** — `metrics.py`: added `save_to_file()` (atomic `os.replace`) and `load_from_file()` (silent on missing/corrupt); `main.py`: `_metrics_snapshot_loop()` background task, load on startup + save on shutdown, guarded by `metrics_snapshot_interval > 0`; path derived as `{log_dir}/metrics.json`
- **SQLite per-request history** — `db.py` (new file): `MetricsDB` backed by `aiosqlite`; schema: `requests` table with indices on `ts` and `model`; `insert_request()` called via `asyncio.create_task()` (fire-and-forget) from `_on_stream_finish` and non-streaming `finally` in both `chat_completions` and `completions`; path derived as `{log_dir}/metrics.db`
- **`GET /metrics/history`** — new endpoint; delegates to `MetricsDB.query_history(hours, model)`; returns 503 if DB unavailable
- `config.py`: added `metrics_snapshot_interval: int = Field(60)`
- `requirements.txt`: added `aiosqlite==0.20.0`
- `.env.example`: added `ORCHESTRATOR_METRICS_SNAPSHOT_INTERVAL`
- Version bumped to 0.6.0
- Updated README and CLAUDE.md: new file layout, endpoint, env var, metrics persistence section, updated known limitations (streaming limitation reframed, metrics persistence removed), updated future plans

### 2026-04-11 (session 7)
- Phase 7 complete — request queuing + force-unload
- **Request queuing / swap timeout** — `process_manager.py`: `ensure_model()` now checks `swap_queue_depth` (reject immediately if queue full) and wraps `_ensure_model_locked()` with `asyncio.wait_for(timeout=swap_timeout_seconds)`; `_ensure_model_locked()` is new method containing the old locked body; `_queue_waiters` counter added and exposed in `get_status()`; `main.py`: `orc_error_handler` adds `Retry-After: 5` header for `swap_timeout` and `swap_queue_full` error types
- **Force-unload** — `process_manager.py`: `force_kill()` sends SIGKILL immediately, cancels stderr reader, resets state without lock or delay, fires background `_reap_process()` task; `main.py`: `POST /admin/force-unload` endpoint added
- `config.py`: added `swap_timeout_seconds: int = Field(30)` and `swap_queue_depth: int = Field(0)`
- `.env.example`: added `ORCHESTRATOR_SWAP_TIMEOUT_SECONDS` and `ORCHESTRATOR_SWAP_QUEUE_DEPTH`
- Version bumped to 0.7.0
- Updated README and CLAUDE.md: new endpoints, env vars, error types, future plans pruned

### 2026-04-15 (session 8)
- First-run setup pass — no Python code changes
- Diagnosed user's report that `.env` wasn't being read: root cause was that `/home/user/Orc/.env` simply did not exist. `config.py` is already correct (`SettingsConfigDict(env_prefix="ORCHESTRATOR_", env_file=".env", env_file_encoding="utf-8")`); pydantic-settings silently falls back to class defaults when the env file is missing, which masked the real issue.
- `README.md`: added a "First-run gotcha" callout under setup step 3 explaining the silent-fallback behavior and giving a `python -c "from config import settings; ..."` verification snippet.
- `profiles.yaml.example`: refreshed the local-spawn examples to current-generation models — `gemma4-e2b`, `gemma4-e4b`, `qwen3.5-4b`, `qwen3.5-9b`; remote-backend example renamed to `qwen3.5-9b-remote`. Comments note that Gemma 4 E2B/E4B multimodal inputs require `llama-mtmd-cli` (which Orc does not wrap) and that Qwen 3.5 hybrid reasoning defaults to non-thinking mode.
- User's local `.env` and `profiles.yaml` (both gitignored) created from the refreshed templates for this workstation.

### 2026-04-16 (session 9)
- Phase 8 complete — LLM output quality safeguards
- **Sampling defaults** — `profiles.yaml.example`: added `repeat_penalty: 1.1` and `max_tokens: 2048` to all four profiles' `sampling_defaults`
- **finish_reason extraction** — `proxy.py`: parsed from non-streaming responses and from last SSE chunk in streaming; `on_finish` callback expanded to `(pt, ct, finish_reason)`; wired into all 4 handler paths in `main.py`
- **Empty response detection** — `proxy.py`: warning log on `completion_tokens == 0`; `main.py`: `X-Orc-Warning: empty-response` and `X-Orc-Warning: generation-truncated` headers on non-streaming responses
- **Repetition detection** — `proxy.py`: `detect_repetition()` sliding-window function; streaming `_gen()` checks every 20 chunks; abort mode injects error SSE sentinel; warn mode logs only; non-streaming path checks post-hoc with `X-Orc-Warning: repetition-detected` header
- **Metrics** — `metrics.py`: `empty_responses` counter and `finish_reason_counts` dict added to `ModelMetrics`; Prometheus metrics updated; JSON snapshot round-trip updated
- **DB** — `db.py`: `finish_reason TEXT` column with ALTER TABLE migration; `query_history()` includes finish_reason distribution
- **Config** — `config.py`: `repeat_detection_window`, `repeat_detection_threshold`, `repeat_detection_action`; `.env.example` updated
- **Tests** — 73 tests total (17 new); covers detect_repetition, streaming abort/warn/disabled, finish_reason, empty responses, snapshot round-trip
- Version bumped to 0.8.0
- Updated README.md: new env vars, error type, output quality safeguards section
- Updated CLAUDE.md: Phase 8 description, session log
