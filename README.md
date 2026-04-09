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
   apps ──▶ │  Orc (FastAPI)          │ ──▶ spawns / kills llama-server.exe (local)
            │  main.py                │      — OR —
            └─────────────────────────┘      routes to remote backends (multi-backend)
                       │
                       └─── proxies ────▶ llama-server  (local or remote)
```

**Local mode (default):** One model in VRAM at a time. On model switch, the running child is killed, a post-kill delay allows VRAM to drain, then the new child is spawned.

**Remote-backend mode:** The profile lists one or more external `llama-server` URLs. Orc routes requests to them round-robin. No local process is managed.

---

## File layout

```
main.py               FastAPI app, routes, middleware, lifespan, exception handler
config.py             Env var loading (pydantic-settings)
metrics.py            MetricsStore — in-process request and spawn counters
profiles.py           YAML profile loader, Pydantic models, CLI-arg builder
process_manager.py    Spawn/kill state machine, stderr capture (OrcError lives here)
proxy.py              HTTP proxy to child or remote backend, error classification
profiles.yaml         Your model profiles (gitignored — copy from profiles.yaml.example)
profiles.yaml.example Template with local and remote-backend examples
.env                  Your env vars (gitignored — copy from .env.example)
.env.example          All supported env vars with defaults
requirements.txt      Python dependencies
logs/                 Rolling log files (created on first run)
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

Edit `profiles.yaml` to point `model_path` at your actual `.gguf` files, or configure `backends` for remote mode. The key under `models:` is the model ID used in API requests.

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

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness check — always returns `{"status": "ok"}` |
| GET | `/status` | Current state, loaded model ID, child PID |
| GET | `/v1/models` | List all profiles (`backend_mode`: `"local"` or `"remote"`) |
| POST | `/v1/chat/completions` | OpenAI-compatible, streaming and non-streaming |
| GET | `/metrics` | Per-model request counters + process-level spawn/kill stats |
| POST | `/admin/load` | Pre-load a model into VRAM (requires `X-Admin-Key`) |
| POST | `/admin/unload` | Unload the running model (requires `X-Admin-Key`) |
| POST | `/admin/custom_run` | Spawn with flag overrides (requires `X-Admin-Key`) |

### Session IDs

Every request is tagged with a session ID. Orc reads the `X-Session-ID` request header if present; otherwise it generates a random UUID. The ID is echoed back in the `X-Session-ID` response header and included in every line of `logs/requests.jsonl`.

### Admin endpoints

All `/admin/*` routes require an `X-Admin-Key` header matching `ORCHESTRATOR_ADMIN_KEY`.  
If `ORCHESTRATOR_ADMIN_KEY` is not set, all admin routes return `503`.

**`POST /admin/load`** — body: `{"model": "<model-id>"}`  
Pre-loads a local-spawn model. For remote-backend profiles returns immediately.

**`POST /admin/unload`** — no body required  
Unloads the currently running local model.

**`POST /admin/custom_run`** — body: `{"model": "<model-id>", "flags": {...}}`  
Kills whatever is running, merges `flags` on top of the profile's flags, and spawns the model with the combined flag set. Useful for quick experiments without editing `profiles.yaml`. Local-spawn profiles only.

---

## Profile YAML schema

### Local spawn profile

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
    sampling_defaults:           # documented only — NOT auto-merged into requests
      temperature: 0.2
      top_p: 0.9
    chat_template: null          # null = auto-detect from GGUF metadata
```

Flag-to-CLI-arg rules: `_` → `-`, prepend `--`. `true` emits the flag with no value. `false` omits the flag entirely. `0` is a valid value and is not omitted.

### Remote backend profile

```yaml
models:
  <model-id>:
    display_name: "Human label"
    backends:
      - url: "http://10.0.0.1:8090"
      - url: "http://10.0.0.2:8090"
    # model_path and flags are ignored in remote mode
```

Orc picks backends round-robin. No local process is spawned. `model_path` is not required.

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

The `stderr_tail` array contains the last lines emitted to `llama-server` stderr before the failure — the primary diagnostic tool. For remote-backend profiles, `stderr_tail` is always empty (no local process).

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

## Logging

On startup, Orc creates the `logs/` directory (configurable via `ORCHESTRATOR_LOG_DIR`) and opens two rolling files:

| File | Contents | Rotation |
|------|----------|----------|
| `logs/orc.log` | Full application log at INFO level | Daily, 14-day retention |
| `logs/requests.jsonl` | One JSON object per `/v1/chat/completions` request | Daily, 14-day retention |

Each line in `requests.jsonl`:
```json
{"session_id": "...", "model": "...", "stream": false, "latency_ms": 123.4, "status": 200, "prompt_tokens": 512, "completion_tokens": 64}
```

For streaming requests, `prompt_tokens` and `completion_tokens` are `0` (headers are committed before the stream completes).

---

## Metrics

`GET /metrics` returns a JSON snapshot of in-process counters. Counters reset on restart.

```json
{
  "models": {
    "qwen2.5-14b-q5": {
      "requests": 42,
      "prompt_tokens": 18000,
      "completion_tokens": 3200,
      "errors": 1,
      "avg_latency_ms": 2340.5
    }
  },
  "process": {
    "spawns": 3,
    "kills": 2,
    "current_model": "qwen2.5-14b-q5",
    "current_model_uptime_s": 180.3
  }
}
```

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
| `IDLE_TTL_SECONDS` | `600` | Idle eviction timeout (0 = disabled) |
| `ADMIN_KEY` | *(none)* | Required for `/admin/*` routes |
| `LOG_DIR` | `logs` | Directory for rolling log files |

---

## Known limitations

- **Sampling defaults not merged.** The `sampling_defaults` block in profiles is informational; clients must send their own sampling parameters.
- **Profiles loaded once at startup.** Restart the server to pick up `profiles.yaml` changes.
- **Streaming mid-stream errors.** If the child dies after the first SSE chunk is sent, the client receives an incomplete stream (HTTP headers are already committed). Pre-stream errors (connection failure, non-200 status) are still surfaced as structured JSON.
- **Streaming token counts not logged.** `prompt_tokens`/`completion_tokens` are `0` in `requests.jsonl` for streaming requests.
- **No backend health checking.** For remote-backend profiles, unhealthy backends stay in the round-robin rotation until they respond. A failed request to a backend returns an error to the client.
- **Metrics not persisted.** In-process counters reset on restart.
