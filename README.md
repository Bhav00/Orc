# Orc — llama-server Orchestrator

A thin FastAPI layer over `llama-server.exe` (llama.cpp) that gives downstream apps a single OpenAI-compatible endpoint with per-model static profiles and structured error surfacing.

> **Maintenance rule:** Keep this file up to date. Update after every session if behavior, endpoints, config, or the file layout change.

---

## Why this exists

Ollama swallows llama-server errors and returns empty `200` responses. Orc captures `llama-server` stderr in a rolling buffer and returns it in every error response, so you know exactly what went wrong (context overflow, CUDA OOM, bad template, etc.).

---

## Target environment

- **OS:** Windows
- **GPU:** Single NVIDIA (tested on V100 32 GB)
- **Binary:** llama.cpp prebuilt CUDA `llama-server.exe`
- **Python:** 3.11+

---

## Architecture

```
            ┌─────────────────────────┐
   apps ──▶ │  Orc (FastAPI)          │ ──▶ spawns / kills llama-server.exe
            │  main.py                │      one at a time
            └─────────────────────────┘             │
                       │                            ▼
                       │                  ┌──────────────────────┐
                       └─── proxies ────▶ │  llama-server.exe    │
                                          │  127.0.0.1:CHILD_PORT│
                                          └──────────────────────┘
```

**One model in VRAM at a time.** On model switch, the running child is killed, a post-kill delay allows VRAM to drain, then the new child is spawned.

---

## File layout

```
main.py               FastAPI app, routes, lifespan, exception handler
config.py             Env var loading (pydantic-settings)
profiles.py           YAML profile loader, Pydantic models, CLI-arg builder
process_manager.py    Spawn/kill state machine, stderr capture (OrcError lives here)
proxy.py              HTTP proxy to child, error classification
profiles.yaml         Your model profiles (gitignored — copy from profiles.yaml.example)
profiles.yaml.example Template with two example Qwen2.5 profiles
.env                  Your env vars (gitignored — copy from .env.example)
.env.example          All supported env vars with defaults
requirements.txt      Python dependencies
```

---

## Setup

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Create your profiles file

```
cp profiles.yaml.example profiles.yaml
```

Edit `profiles.yaml` to point `model_path` at your actual `.gguf` files. The key under `models:` is the model ID used in API requests.

### 3. Create your env file

```
cp .env.example .env
```

At minimum set `ORCHESTRATOR_LLAMA_SERVER_BIN` to the full path of your `llama-server.exe`.

### 4. Run

```
python main.py
```

Or with uvicorn directly:

```
uvicorn main:app --host 127.0.0.1 --port 8080
```

---

## Endpoints (Phase 1)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness check — always returns `{"status": "ok"}` |
| GET | `/status` | Current state, loaded model ID, child PID |
| GET | `/v1/models` | List all profiles from `profiles.yaml` |
| POST | `/v1/chat/completions` | OpenAI-compatible, non-streaming (Phase 1) |

Phase 2 will add streaming, idle reaper, and admin endpoints (`/admin/load`, `/admin/unload`). Phase 3 adds `/admin/custom_run`.

---

## Profile YAML schema

```yaml
models:
  <model-id>:                    # used as the "model" field in API requests
    display_name: "Human label"
    model_path: "C:/models/file.gguf"
    estimated_vram_mb: 12000     # pre-spawn sanity check only, not measured
    flags:
      ctx_size: 8192             # → --ctx-size 8192
      n_gpu_layers: 99           # → --n-gpu-layers 99
      flash_attn: true           # → --flash-attn  (boolean: true=flag only, false=omit)
      cache_type_k: q8_0         # → --cache-type-k q8_0
      parallel: 1
      # any llama-server flag works here
    sampling_defaults:           # documented only — NOT auto-merged in Phase 1
      temperature: 0.2
      top_p: 0.9
    chat_template: null          # null = auto-detect from GGUF metadata
```

Flag-to-CLI-arg rules: `_` → `-`, prepend `--`. `true` emits the flag with no value. `false` omits the flag entirely. `0` is a valid value and is not omitted.

---

## Error responses

All errors use this shape:

```json
{
  "error": {
    "message": "Context length exceeded",
    "type": "context_length_exceeded",
    "code": "context_length_exceeded"
  },
  "stderr_tail": [
    "llama_decode: input too large (4200 > 4096)",
    "..."
  ]
}
```

The `stderr_tail` array contains the last lines emitted to `llama-server` stderr before the failure — the primary diagnostic tool.

**Classified errors:**

| Pattern in stderr | HTTP status | type |
|-------------------|-------------|------|
| "context window" / "kv cache is full" | 400 | `context_length_exceeded` |
| "out of memory" / OOM | 503 | `out_of_memory` |
| "cuda error" | 503 | `cuda_error` |
| Unknown model ID | 404 | `orchestrator_error` |
| Spawn timeout | 503 | `spawn_timeout` |
| Port in use | 503 | `port_in_use` |

---

## Environment variables

All variables are prefixed `ORCHESTRATOR_`. Defaults are shown.

| Variable | Default | Description |
|----------|---------|-------------|
| `PROFILES_PATH` | `profiles.yaml` | Path to profiles YAML |
| `LLAMA_SERVER_BIN` | `llama-server.exe` | Full path to binary |
| `CHILD_PORT` | `8090` | Internal port for the child process |
| `HOST` | `127.0.0.1` | Orchestrator listen address |
| `PORT` | `8080` | Orchestrator listen port |
| `VRAM_TOTAL_MB` | `32000` | Total GPU VRAM for sanity check |
| `VRAM_RESERVE_MB` | `2000` | VRAM headroom to keep free |
| `SPAWN_TIMEOUT_SECONDS` | `60` | Max wait for child to become healthy |
| `POST_KILL_DELAY_SECONDS` | `2.0` | Sleep after kill before next spawn |
| `IDLE_TTL_SECONDS` | `600` | *(Phase 2)* Idle eviction timeout |
| `ADMIN_KEY` | *(none)* | *(Phase 2)* Required for `/admin/*` routes |

---

## Known limitations (Phase 1)

- **Non-streaming only.** `stream: true` requests are silently forced to `stream: false`.
- **No idle eviction.** The model stays loaded until the next model-switch or server restart.
- **Sampling defaults not merged.** The `sampling_defaults` block in profiles is informational; clients must send their own sampling parameters.
- **Profiles loaded once at startup.** Restart the server to pick up `profiles.yaml` changes.

---

## Future / next iterations

### Session-scoped logging with rolling log files

Every request should be assigned a `session_id` (or accept one via header, e.g. `X-Session-ID`). All log lines — orchestrator events, proxied request metadata, child stderr lines — should be tagged with that ID so a single session's full trace can be pulled from the log file.

Log files should roll on size and/or date (e.g. `logs/orc-2026-04-08.log`, max 50 MB, keep last 14 days). Python's `logging.handlers.TimedRotatingFileHandler` or `RotatingFileHandler` covers this. The in-memory stderr deque stays as the fast-path for error responses; the log files are the durable audit trail.

Things to log per request: session ID, model ID, request timestamp, prompt token count (from response usage field), completion token count, latency ms, HTTP status returned to client, and any stderr lines emitted during that request.

### Usage metrics

Currently nothing is counted. At minimum the orchestrator should track:

- **Per-model:** total requests, total prompt tokens, total completion tokens, total errors, average latency
- **Per-session (if session IDs are implemented):** same breakdown scoped to the session
- **Process-level:** number of spawns, number of kills, total VRAM-load time, model currently loaded + how long it has been loaded

These should be exposed on a `GET /metrics` endpoint — either Prometheus format (for scraping) or a simple JSON summary. No external dependency is required for the JSON path; Prometheus export can use the `prometheus-client` library when needed.

Counters should survive model swaps (i.e. live on the orchestrator, not the child process). They reset on orchestrator restart unless persisted, which is fine for v1.

### Multi-backend / load balancing

The current design assumes a single `llama-server.exe` on a single machine. The next step is to allow the profile registry to list multiple backend URLs instead of always spawning a local process. Each backend is a running `llama-server` instance, possibly on a different machine or port, and Orc acts as a routing layer in front of them.

Proposed profile extension:
```yaml
models:
  qwen2.5-14b-q5:
    backends:
      - url: "http://10.0.0.1:8090"
      - url: "http://10.0.0.2:8090"
    # ... rest of profile unchanged
```

When multiple backends are listed for a model, Orc load-balances across them (round-robin or least-connections). For Phase 1 compatibility, a profile with no `backends` key falls back to the current local-spawn behaviour.

**Same models on all endpoints:** for now the assumption is that every backend listed under a model profile actually has that model loaded or can load it. A model availability checker (health + model-list probe against each backend) is already planned as a separate pipeline component and will feed into backend selection here. Backends that fail the health check are removed from rotation until they recover.
