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
