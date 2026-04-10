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

## Current phase: All phases complete

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

---

## File map

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, routes, `SessionMiddleware`, `BackendRouter`, lifespan, `OrcError` exception handler |
| `config.py` | `Settings` (pydantic-settings), module-level `settings` singleton |
| `metrics.py` | `MetricsStore` — in-process per-model and process-level counters |
| `profiles.py` | `ModelProfile`, `BackendEntry`, `ProfilesFile`, `load_profiles()`, `build_cli_args()` |
| `process_manager.py` | `ChildState` enum, `OrcError` exception, `ChildInfo` dataclass, `ProcessManager` class |
| `proxy.py` | `classify_stderr()`, `proxy_chat_completions()`, `proxy_chat_completions_stream()` |
| `profiles.yaml.example` | Template profile file — copy to `profiles.yaml` |
| `.env.example` | All env vars with defaults — copy to `.env` |
| `requirements.txt` | Python deps |

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
- `LOG_DIR` — directory for rolling log files (default `logs`)

---

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/healthz` | Always `{"status": "ok"}` |
| GET | `/status` | State machine status + PID |
| GET | `/v1/models` | Lists profiles from YAML; includes `backend_mode` field |
| POST | `/v1/chat/completions` | Streaming and non-streaming |
| GET | `/metrics` | Per-model + process-level counters (JSON) |
| POST | `/admin/load` | Pre-load model; requires `X-Admin-Key` |
| POST | `/admin/unload` | Unload model; requires `X-Admin-Key` |
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

`claude/review-docs-improvements-vNmMT`

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
- Catalogued 15 future improvement suggestions across 3 effort tiers (see plan file)
