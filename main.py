import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import settings
from process_manager import OrcError, ProcessManager
from profiles import load_profiles
from proxy import proxy_chat_completions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
log = logging.getLogger("orc.main")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    # All extra fields (temperature, top_p, max_tokens, tools, etc.) are
    # preserved and forwarded as-is to the child.
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Loading profiles from %s", settings.profiles_path)
    profiles = load_profiles(settings.profiles_path)
    log.info("Loaded %d model profile(s): %s", len(profiles.models), list(profiles.models.keys()))

    pm = ProcessManager(settings)
    pm.set_profiles(profiles)
    app.state.process_manager = pm
    app.state.profiles = profiles

    yield

    log.info("Shutting down — unloading any running model")
    await pm.kill_current()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Orc", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(OrcError)
async def orc_error_handler(request: Request, exc: OrcError) -> JSONResponse:
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
            }
            for model_id, profile in profiles.models.items()
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request) -> dict:
    pm: ProcessManager = request.app.state.process_manager

    await pm.ensure_model(body.model)

    return await proxy_chat_completions(
        request_body=body.model_dump(),
        process_manager=pm,
        child_port=settings.child_port,
    )


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
