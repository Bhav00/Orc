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


def detect_repetition(text: str, threshold: int = 4, min_len: int = 10) -> str | None:
    """Return the repeated pattern if found in *text*, else None.

    Checks whether any substring of length *min_len* .. len(text)//threshold
    appears *threshold* or more times consecutively.
    """
    if len(text) < min_len * threshold:
        return None
    max_pat = len(text) // threshold
    for pat_len in range(min_len, max_pat + 1):
        pat = text[:pat_len]
        if pat * threshold in text:
            return pat
    return None


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
        data = resp.json()
        choices = data.get("choices") or []
        usage = data.get("usage") or {}
        ct = usage.get("completion_tokens", 0)
        finish = choices[0].get("finish_reason") if choices else None
        if not choices or ct == 0:
            log.warning(
                "Empty response from %s (completion_tokens=%d, finish_reason=%s)",
                target_url, ct, finish,
            )
        return data

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
    on_finish: Callable[[int, int, str | None], None] | None = None,
    repeat_window: int = 0,
    repeat_threshold: int = 4,
    repeat_action: str = "abort",
) -> AsyncGenerator[bytes, None]:
    """Initiate a streaming request to target_url.

    Establishes the connection and checks the status code before returning, so
    OrcError can still be raised and caught by FastAPI's exception handler.
    Returns an async generator that yields raw SSE byte chunks.

    If *on_finish* is provided, it is called with
    (prompt_tokens, completion_tokens, finish_reason) after the stream is
    fully consumed.  Token counts and finish_reason are extracted from the
    last SSE ``data:`` line (llama-server includes ``usage`` in the final chunk).

    When *repeat_window* > 0, a sliding-window repetition detector runs on
    streamed content deltas.  If triggered, behaviour depends on
    *repeat_action*: ``"abort"`` injects an error SSE sentinel and closes;
    ``"warn"`` logs but continues.
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
        recent_text = ""
        chunk_count = 0
        repetition_triggered = False

        try:
            async for chunk in response.aiter_bytes():
                yield chunk
                text = chunk.decode("utf-8", errors="replace")

                for line in text.split("\n"):
                    stripped = line.strip()
                    if not stripped.startswith("data: ") or stripped == "data: [DONE]":
                        continue
                    payload = stripped[6:]
                    last_data_line = payload

                    if repeat_window > 0 and not repetition_triggered:
                        try:
                            parsed = json.loads(payload)
                            choices = parsed.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                content = delta.get("content", "")
                                if content:
                                    recent_text += content
                                    if len(recent_text) > repeat_window:
                                        recent_text = recent_text[-repeat_window:]
                                    chunk_count += 1
                                    if chunk_count % 20 == 0:
                                        pat = detect_repetition(recent_text, repeat_threshold)
                                        if pat:
                                            log.warning(
                                                "Repetition detected mid-stream: pattern=%r",
                                                pat[:80],
                                            )
                                            repetition_triggered = True
                                            if repeat_action == "abort":
                                                error_payload = json.dumps({
                                                    "error": {
                                                        "message": "Generation aborted: repetitive output detected",
                                                        "type": "repetition_detected",
                                                        "code": "repetition_detected",
                                                    },
                                                })
                                                yield f"data: {error_payload}\n\n".encode()
                                                yield b"data: [DONE]\n\n"
                                                return
                        except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                            pass

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
                finish_reason = None
                if last_data_line:
                    try:
                        parsed = json.loads(last_data_line)
                        usage = parsed.get("usage") or {}
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                        choices = parsed.get("choices") or []
                        if choices:
                            finish_reason = choices[0].get("finish_reason")
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                on_finish(prompt_tokens, completion_tokens, finish_reason)

    return _gen()
