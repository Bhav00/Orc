import pytest
import httpx
import respx

from process_manager import OrcError
from proxy import classify_stderr, detect_repetition, proxy_chat_completions, proxy_chat_completions_stream


# ---------------------------------------------------------------------------
# classify_stderr
# ---------------------------------------------------------------------------

class TestClassifyStderr:
    def test_context_window(self):
        status, etype, _ = classify_stderr(["context window exceeded"])
        assert status == 400
        assert etype == "context_length_exceeded"

    def test_kv_cache_full(self):
        status, etype, _ = classify_stderr(["kv cache is full"])
        assert status == 400
        assert etype == "context_length_exceeded"

    def test_out_of_memory(self):
        status, etype, _ = classify_stderr(["CUDA out of memory"])
        assert status == 503
        assert etype == "out_of_memory"

    def test_cudaoutofmemory(self):
        status, etype, _ = classify_stderr(["CudaOutOfMemory detected"])
        assert status == 503
        assert etype == "out_of_memory"

    def test_oom_token(self):
        status, etype, _ = classify_stderr(["allocation failed oom"])
        assert status == 503
        assert etype == "out_of_memory"

    def test_cuda_error(self):
        status, etype, _ = classify_stderr(["cuda error: device-side assert"])
        assert status == 503
        assert etype == "cuda_error"

    def test_generic_fallback(self):
        status, etype, _ = classify_stderr(["some unknown error"])
        assert status == 503
        assert etype == "child_error"

    def test_empty_input(self):
        status, etype, _ = classify_stderr([])
        assert status == 503
        assert etype == "child_error"

    def test_case_insensitive(self):
        status, etype, _ = classify_stderr(["CONTEXT WINDOW EXCEEDED"])
        assert status == 400
        assert etype == "context_length_exceeded"


# ---------------------------------------------------------------------------
# proxy_chat_completions (non-streaming)
# ---------------------------------------------------------------------------

class TestProxyChatCompletions:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success_returns_json(self):
        expected = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        respx.post("http://localhost:8090/v1/chat/completions").respond(200, json=expected)

        result = await proxy_chat_completions(
            request_body={"model": "test", "messages": []},
            target_url="http://localhost:8090",
        )
        assert result == expected

    @pytest.mark.asyncio
    @respx.mock
    async def test_connect_error_raises_child_unreachable(self):
        respx.post("http://localhost:8090/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(OrcError) as exc_info:
            await proxy_chat_completions(
                request_body={"model": "test", "messages": []},
                target_url="http://localhost:8090",
            )
        assert exc_info.value.status_code == 503
        assert exc_info.value.error_type == "child_unreachable"

    @pytest.mark.asyncio
    @respx.mock
    async def test_read_timeout_raises_child_timeout(self):
        respx.post("http://localhost:8090/v1/chat/completions").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        with pytest.raises(OrcError) as exc_info:
            await proxy_chat_completions(
                request_body={"model": "test", "messages": []},
                target_url="http://localhost:8090",
            )
        assert exc_info.value.status_code == 504
        assert exc_info.value.error_type == "child_timeout"

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_200_raises_classified_error(self):
        respx.post("http://localhost:8090/v1/chat/completions").respond(
            500, json={"error": {"message": "internal error"}}
        )
        with pytest.raises(OrcError) as exc_info:
            await proxy_chat_completions(
                request_body={"model": "test", "messages": []},
                target_url="http://localhost:8090",
            )
        assert exc_info.value.status_code == 503
        assert exc_info.value.error_type == "child_error"


# ---------------------------------------------------------------------------
# proxy_chat_completions_stream
# ---------------------------------------------------------------------------

class TestEndpointPathParameter:
    @pytest.mark.asyncio
    @respx.mock
    async def test_custom_endpoint_path(self):
        expected = {"choices": [{"text": "hello"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        respx.post("http://localhost:8090/v1/completions").respond(200, json=expected)

        result = await proxy_chat_completions(
            request_body={"model": "test", "prompt": "Say hi"},
            target_url="http://localhost:8090",
            endpoint_path="/v1/completions",
        )
        assert result == expected

    @pytest.mark.asyncio
    @respx.mock
    async def test_default_endpoint_path(self):
        expected = {"choices": [{"message": {"content": "hi"}}]}
        respx.post("http://localhost:8090/v1/chat/completions").respond(200, json=expected)

        result = await proxy_chat_completions(
            request_body={"model": "test", "messages": []},
            target_url="http://localhost:8090",
        )
        assert result == expected


class TestProxyChatCompletionsStream:
    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_connect_error_raises(self):
        respx.post("http://localhost:8090/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(OrcError) as exc_info:
            await proxy_chat_completions_stream(
                request_body={"model": "test", "messages": [], "stream": True},
                target_url="http://localhost:8090",
            )
        assert exc_info.value.status_code == 503
        assert exc_info.value.error_type == "child_unreachable"

    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_non_200_raises(self):
        respx.post("http://localhost:8090/v1/chat/completions").respond(
            500, json={"error": {"message": "bad"}}
        )
        with pytest.raises(OrcError) as exc_info:
            await proxy_chat_completions_stream(
                request_body={"model": "test", "messages": [], "stream": True},
                target_url="http://localhost:8090",
            )
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Streaming token extraction (on_finish callback)
# ---------------------------------------------------------------------------

class TestStreamingUsageExtraction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_on_finish_receives_token_counts_and_finish_reason(self):
        sse_body = (
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post("http://localhost:8090/v1/chat/completions").respond(
            200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )
        received = {}

        def on_finish(pt, ct, fr):
            received["prompt_tokens"] = pt
            received["completion_tokens"] = ct
            received["finish_reason"] = fr

        gen = await proxy_chat_completions_stream(
            request_body={"model": "test", "messages": [], "stream": True},
            target_url="http://localhost:8090",
            on_finish=on_finish,
        )
        async for _ in gen:
            pass

        assert received["prompt_tokens"] == 10
        assert received["completion_tokens"] == 2
        assert received["finish_reason"] == "stop"

    @pytest.mark.asyncio
    @respx.mock
    async def test_on_finish_no_usage_returns_zeros(self):
        sse_body = (
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post("http://localhost:8090/v1/chat/completions").respond(
            200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )
        received = {}

        def on_finish(pt, ct, fr):
            received["prompt_tokens"] = pt
            received["completion_tokens"] = ct
            received["finish_reason"] = fr

        gen = await proxy_chat_completions_stream(
            request_body={"model": "test", "messages": [], "stream": True},
            target_url="http://localhost:8090",
            on_finish=on_finish,
        )
        async for _ in gen:
            pass

        assert received["prompt_tokens"] == 0
        assert received["completion_tokens"] == 0
        assert received["finish_reason"] is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_on_finish_not_called_when_none(self):
        sse_body = 'data: {"choices":[]}\n\ndata: [DONE]\n\n'
        respx.post("http://localhost:8090/v1/chat/completions").respond(
            200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )
        gen = await proxy_chat_completions_stream(
            request_body={"model": "test", "messages": [], "stream": True},
            target_url="http://localhost:8090",
            on_finish=None,
        )
        # Should not raise — on_finish is None
        async for _ in gen:
            pass


# ---------------------------------------------------------------------------
# detect_repetition
# ---------------------------------------------------------------------------

class TestDetectRepetition:
    def test_no_repetition(self):
        assert detect_repetition("The quick brown fox jumps over the lazy dog.") is None

    def test_short_text_no_false_positive(self):
        assert detect_repetition("abc", threshold=4, min_len=10) is None

    def test_repeated_phrase(self):
        pattern = "I hope this helps! "
        text = pattern * 5
        result = detect_repetition(text, threshold=4, min_len=10)
        assert result is not None

    def test_threshold_not_met(self):
        pattern = "Hello world! "
        text = pattern * 3
        assert detect_repetition(text, threshold=4, min_len=10) is None

    def test_short_pattern_below_min_len(self):
        text = "ab" * 3
        assert detect_repetition(text, threshold=4, min_len=10) is None

    def test_exact_threshold(self):
        pattern = "repeating phrase "
        text = pattern * 4
        result = detect_repetition(text, threshold=4, min_len=10)
        assert result is not None


# ---------------------------------------------------------------------------
# Streaming repetition detection
# ---------------------------------------------------------------------------

class TestStreamingRepetitionDetection:
    @pytest.mark.asyncio
    @respx.mock
    async def test_repetition_abort(self):
        """When repetition is detected and action=abort, stream should end with error SSE."""
        repeated = "I hope this helps! "
        # Build enough SSE chunks to trigger detection (check runs every 20 chunks)
        chunks = []
        for i in range(25):
            chunk = f'data: {{"choices":[{{"delta":{{"content":"{repeated}"}}}}]}}\n\n'
            chunks.append(chunk)
        chunks.append("data: [DONE]\n\n")
        sse_body = "".join(chunks)

        respx.post("http://localhost:8090/v1/chat/completions").respond(
            200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )

        collected = []
        gen = await proxy_chat_completions_stream(
            request_body={"model": "test", "messages": [], "stream": True},
            target_url="http://localhost:8090",
            repeat_window=200,
            repeat_threshold=4,
            repeat_action="abort",
        )
        async for chunk in gen:
            collected.append(chunk.decode("utf-8", errors="replace"))

        combined = "".join(collected)
        assert "repetition_detected" in combined

    @pytest.mark.asyncio
    @respx.mock
    async def test_repetition_warn_continues(self):
        """When action=warn, stream should continue even after repetition."""
        repeated = "I hope this helps! "
        chunks = []
        for i in range(25):
            chunk = f'data: {{"choices":[{{"delta":{{"content":"{repeated}"}}}}]}}\n\n'
            chunks.append(chunk)
        chunks.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":25}}\n\n')
        chunks.append("data: [DONE]\n\n")
        sse_body = "".join(chunks)

        respx.post("http://localhost:8090/v1/chat/completions").respond(
            200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )

        received = {}

        def on_finish(pt, ct, fr):
            received["prompt_tokens"] = pt
            received["completion_tokens"] = ct

        gen = await proxy_chat_completions_stream(
            request_body={"model": "test", "messages": [], "stream": True},
            target_url="http://localhost:8090",
            on_finish=on_finish,
            repeat_window=200,
            repeat_threshold=4,
            repeat_action="warn",
        )
        collected = []
        async for chunk in gen:
            collected.append(chunk.decode("utf-8", errors="replace"))

        combined = "".join(collected)
        # Should NOT contain error sentinel
        assert "repetition_detected" not in combined
        # on_finish should still be called with token counts
        assert received["prompt_tokens"] == 5

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_detection_when_disabled(self):
        """With repeat_window=0, no detection should occur."""
        repeated = "I hope this helps! "
        chunks = []
        for i in range(25):
            chunk = f'data: {{"choices":[{{"delta":{{"content":"{repeated}"}}}}]}}\n\n'
            chunks.append(chunk)
        chunks.append("data: [DONE]\n\n")
        sse_body = "".join(chunks)

        respx.post("http://localhost:8090/v1/chat/completions").respond(
            200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )

        gen = await proxy_chat_completions_stream(
            request_body={"model": "test", "messages": [], "stream": True},
            target_url="http://localhost:8090",
            repeat_window=0,
        )
        collected = []
        async for chunk in gen:
            collected.append(chunk.decode("utf-8", errors="replace"))

        combined = "".join(collected)
        assert "repetition_detected" not in combined
