import logging

import httpx

from process_manager import ChildState, OrcError, ProcessManager

log = logging.getLogger("orc.proxy")


def classify_stderr(stderr_lines: list[str]) -> tuple[int, str, str]:
    """Scan stderr tail for known error patterns.
    Returns (http_status, error_type, human_message).
    """
    joined = "\n".join(stderr_lines).lower()

    if "context window" in joined or "kv cache is full" in joined:
        return 400, "context_length_exceeded", "Context length exceeded"

    if "out of memory" in joined or "cudaoutofmemory" in joined or " oom" in joined:
        return 503, "out_of_memory", "GPU out of memory"

    if "cuda error" in joined:
        return 503, "cuda_error", "CUDA error in child process"

    return 503, "child_error", "Child process returned an error"


async def proxy_chat_completions(
    request_body: dict,
    process_manager: ProcessManager,
    child_port: int,
) -> dict:
    """Forward a /v1/chat/completions request to the child and return parsed JSON.

    Non-streaming only (Phase 1): forces stream=False regardless of what the
    client sent. Raises OrcError on any failure, with the child's stderr tail
    included for diagnostics.
    """
    # Phase 1: non-streaming only
    request_body = {**request_body, "stream": False}

    url = f"http://127.0.0.1:{child_port}/v1/chat/completions"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=5.0)
    ) as client:
        try:
            resp = await client.post(url, json=request_body)

        except httpx.ConnectError:
            # Child died between ensure_model() and the actual request
            process_manager._state = ChildState.DYING
            stderr = process_manager.get_stderr_tail(30)
            raise OrcError(503, "Child process is unreachable", error_type="child_unreachable", stderr_tail=stderr)

        except httpx.ReadTimeout:
            stderr = process_manager.get_stderr_tail(30)
            raise OrcError(504, "Child process timed out", error_type="child_timeout", stderr_tail=stderr)

        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            # Child reset the connection mid-response (crash, OOM, etc.)
            process_manager._state = ChildState.DYING
            stderr = process_manager.get_stderr_tail(30)
            raise OrcError(
                503,
                f"Child process connection error: {exc}",
                error_type="child_connection_error",
                stderr_tail=stderr,
            )

    # After receiving a response, verify the child is still alive.
    # A crash that produces a response before dying shows up here.
    if not process_manager._is_child_alive():
        stderr = process_manager.get_stderr_tail(30)
        status, etype, emsg = classify_stderr(stderr)
        process_manager._state = ChildState.DYING
        raise OrcError(status, emsg, error_type=etype, stderr_tail=stderr)

    if resp.status_code == 200:
        return resp.json()

    # 4xx / 5xx from the child — classify and surface stderr
    stderr = process_manager.get_stderr_tail(30)
    status, etype, emsg = classify_stderr(stderr)

    # Prefer the child's own error message if it has one
    try:
        child_body = resp.json()
        if isinstance(child_body.get("error"), dict):
            emsg = child_body["error"].get("message", emsg)
    except Exception:
        pass

    log.warning(
        "Child returned HTTP %d. Classified as %s. Last stderr:\n%s",
        resp.status_code,
        etype,
        "\n".join(stderr),
    )
    raise OrcError(status, emsg, error_type=etype, stderr_tail=stderr)
