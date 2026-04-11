import json
import logging
from collections.abc import AsyncGenerator, Callable

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


def _stderr(pm: ProcessManager | None, n: int = 30) -> list[str]:
    return pm.get_stderr_tail(n) if pm is not None else []


async def proxy_chat_completions(
    request_body: dict,
    target_url: str,
    process_manager: ProcessManager | None = None,
    endpoint_path: str = "/v1/chat/completions",
) -> dict:
    """Forward a non-streaming request to target_url.

    Forces stream=False. Raises OrcError on any failure, with the child's
    stderr tail included for diagnostics (empty when process_manager is None).
    """
    request_body = {**request_body, "stream": False}
    url = f"{target_url}{endpoint_path}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0)) as client:
        try:
            resp = await client.post(url, json=request_body)

        except httpx.ConnectError:
            if process_manager is not None:
                process_manager._state = ChildState.DYING
            stderr = _stderr(process_manager)
            raise OrcError(503, "Child process is unreachable", error_type="child_unreachable", stderr_tail=stderr)

        except httpx.ReadTimeout:
            stderr = _stderr(process_manager)
            raise OrcError(504, "Child process timed out", error_type="child_timeout", stderr_tail=stderr)

        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if process_manager is not None:
                process_manager._state = ChildState.DYING
            stderr = _stderr(process_manager)
            raise OrcError(
                503,
                f"Child process connection error: {exc}",
                error_type="child_connection_error",
                stderr_tail=stderr,
            )

    if process_manager is not None and not process_manager._is_child_alive():
        stderr = _stderr(process_manager)
        status, etype, emsg = classify_stderr(stderr)
        process_manager._state = ChildState.DYING
        raise OrcError(status, emsg, error_type=etype, stderr_tail=stderr)

    if resp.status_code == 200:
        return resp.json()

    stderr = _stderr(process_manager)
    status, etype, emsg = classify_stderr(stderr)

    try:
        child_body = resp.json()
        if isinstance(child_body.get("error"), dict):
            emsg = child_body["error"].get("message", emsg)
    except Exception:
        pass

    log.warning(
        "Target %s returned HTTP %d. Classified as %s. Last stderr:\n%s",
        target_url,
        resp.status_code,
        etype,
        "\n".join(stderr),
    )
    raise OrcError(status, emsg, error_type=etype, stderr_tail=stderr)


async def proxy_chat_completions_stream(
    request_body: dict,
    target_url: str,
    process_manager: ProcessManager | None = None,
    endpoint_path: str = "/v1/chat/completions",
    on_finish: Callable[[int, int], None] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Initiate a streaming request to target_url.

    Establishes the connection and checks the status code before returning, so
    OrcError can still be raised and caught by FastAPI's exception handler.
    Returns an async generator that yields raw SSE byte chunks.

    If *on_finish* is provided, it is called with (prompt_tokens, completion_tokens)
    after the stream is fully consumed.  Token counts are extracted from the last
    SSE ``data:`` line (llama-server includes ``usage`` in the final chunk).
    """
    url = f"{target_url}{endpoint_path}"
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0))

    try:
        response = await client.send(
            client.build_request("POST", url, json=request_body),
            stream=True,
        )
    except httpx.ConnectError:
        await client.aclose()
        if process_manager is not None:
            process_manager._state = ChildState.DYING
        stderr = _stderr(process_manager)
        raise OrcError(503, "Child process is unreachable", error_type="child_unreachable", stderr_tail=stderr)
    except httpx.ReadTimeout:
        await client.aclose()
        stderr = _stderr(process_manager)
        raise OrcError(504, "Child process timed out", error_type="child_timeout", stderr_tail=stderr)

    if response.status_code != 200:
        await response.aread()
        await client.aclose()
        stderr = _stderr(process_manager)
        status, etype, emsg = classify_stderr(stderr)
        try:
            body = response.json()
            if isinstance(body.get("error"), dict):
                emsg = body["error"].get("message", emsg)
        except Exception:
            pass
        log.warning(
            "Target %s returned HTTP %d for streaming request. Classified as %s.",
            target_url, response.status_code, etype,
        )
        raise OrcError(status, emsg, error_type=etype, stderr_tail=stderr)

    async def _gen() -> AsyncGenerator[bytes, None]:
        last_data_line = ""
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
                # Track the last SSE data line for usage extraction
                if on_finish is not None:
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.split("\n"):
                        stripped = line.strip()
                        if stripped.startswith("data: ") and stripped != "data: [DONE]":
                            last_data_line = stripped[6:]
        except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
            if process_manager is not None:
                process_manager._state = ChildState.DYING
            stderr = _stderr(process_manager)
            log.warning("Streaming connection lost mid-stream: %s", exc)
            error_payload = json.dumps({
                "error": {
                    "message": "Stream interrupted: connection lost mid-stream",
                    "type": "child_connection_error",
                    "code": "child_connection_error",
                },
                "stderr_tail": stderr,
            })
            yield f"data: {error_payload}\n\n".encode()
        finally:
            await response.aclose()
            await client.aclose()
            if on_finish is not None:
                prompt_tokens = completion_tokens = 0
                if last_data_line:
                    try:
                        parsed = json.loads(last_data_line)
                        usage = parsed.get("usage") or {}
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                on_finish(prompt_tokens, completion_tokens)

    return _gen()
