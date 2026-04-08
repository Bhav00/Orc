# CLAUDE.md — Session context for Orc

> **Mandatory rule:** At the end of every session, update both `README.md` and `CLAUDE.md` to reflect any changes made (new endpoints, changed behavior, added files, config changes, phase completions, known issues discovered).

---

## What this project is

Orc is a thin FastAPI orchestrator that wraps `llama-server.exe` (llama.cpp). It:
- Exposes a single OpenAI-compatible API endpoint for all downstream apps
- Manages one `llama-server.exe` child process at a time (swap on model change)
- Captures child stderr and returns it in structured error responses (the core value prop)

Target: Windows, single NVIDIA GPU, llama.cpp prebuilt CUDA binaries.

---

## Current phase: Phase 2 complete

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

**Not yet implemented (Phase 3):**
- `/admin/custom_run` (ad-hoc flag set)

---

## File map

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, routes, lifespan, `OrcError` exception handler |
| `config.py` | `Settings` (pydantic-settings), module-level `settings` singleton |
| `profiles.py` | `ModelProfile`, `ProfilesFile`, `load_profiles()`, `build_cli_args()` |
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

---

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/healthz` | Always `{"status": "ok"}` |
| GET | `/status` | State machine status + PID |
| GET | `/v1/models` | Lists profiles from YAML |
| POST | `/v1/chat/completions` | Streaming and non-streaming |
| POST | `/admin/load` | Pre-load model; requires `X-Admin-Key` |
| POST | `/admin/unload` | Unload model; requires `X-Admin-Key` |

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

`claude/complete-readme-phases-zR4aB`

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
