import asyncio
import enum
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from config import Settings
from profiles import ModelProfile, ProfilesFile, build_cli_args

log = logging.getLogger("orc.process_manager")


class OrcError(Exception):
    """Structured error raised by the orchestrator. Carries an HTTP status code
    and an optional tail of the child's stderr for diagnostic surfacing."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str = "orchestrator_error",
        code: str | None = None,
        stderr_tail: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code or error_type
        self.stderr_tail: list[str] = stderr_tail or []


class ChildState(enum.Enum):
    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    DYING = "dying"


@dataclass
class ChildInfo:
    model_id: str
    process: asyncio.subprocess.Process
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=100))
    _stderr_reader_task: asyncio.Task | None = None


class ProcessManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state: ChildState = ChildState.IDLE
        self._child: ChildInfo | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._profiles: ProfilesFile | None = None
        self._last_used_at: float | None = None
        self._reaper_task: asyncio.Task | None = None

    def set_profiles(self, profiles: ProfilesFile) -> None:
        self._profiles = profiles

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_model(self, model_id: str) -> None:
        """Ensure the named model is READY. Raises OrcError on failure.
        Called before every proxied request.
        """
        # Fast path: already ready with the right model and still alive
        if (
            self._state == ChildState.READY
            and self._child is not None
            and self._child.model_id == model_id
            and self._is_child_alive()
        ):
            self._last_used_at = time.monotonic()
            return

        async with self._lock:
            # Re-check under lock — another coroutine may have done the work
            if (
                self._state == ChildState.READY
                and self._child is not None
                and self._child.model_id == model_id
                and self._is_child_alive()
            ):
                self._last_used_at = time.monotonic()
                return

            # Kill whatever is currently running (any non-IDLE state)
            if self._state != ChildState.IDLE:
                await self._kill_and_wait()

            # Look up profile
            assert self._profiles is not None, "set_profiles() must be called before ensure_model()"
            profile = self._profiles.models.get(model_id)
            if profile is None:
                raise OrcError(404, f"Unknown model: {model_id!r}. Check profiles.yaml.")

            # VRAM sanity check
            headroom = self._settings.vram_total_mb - self._settings.vram_reserve_mb
            if profile.estimated_vram_mb > headroom:
                raise OrcError(
                    503,
                    f"Model {model_id!r} estimated VRAM ({profile.estimated_vram_mb} MB) "
                    f"exceeds available headroom ({headroom} MB).",
                    error_type="insufficient_vram",
                )

            await self._spawn(model_id, profile)
            self._last_used_at = time.monotonic()

    async def kill_current(self) -> None:
        """Force-unload the current model. Safe to call when already IDLE."""
        async with self._lock:
            if self._state != ChildState.IDLE:
                await self._kill_and_wait()

    def start_idle_reaper(self) -> None:
        """Start the background task that evicts idle models. No-op if TTL is 0."""
        if self._settings.idle_ttl_seconds > 0:
            self._reaper_task = asyncio.create_task(
                self._idle_reaper_loop(), name="idle-reaper"
            )

    def stop_idle_reaper(self) -> None:
        """Cancel the idle reaper task."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()

    def get_status(self) -> dict:
        return {
            "state": self._state.value,
            "model_id": self._child.model_id if self._child else None,
            "pid": self._child.process.pid if self._child else None,
            "stderr_tail_lines": len(self._child.stderr_tail) if self._child else 0,
        }

    def get_stderr_tail(self, n: int = 20) -> list[str]:
        if self._child is None:
            return []
        tail = list(self._child.stderr_tail)
        return tail[-n:] if n < len(tail) else tail

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _spawn(self, model_id: str, profile: ModelProfile) -> None:
        # Pre-spawn port check: faster error than waiting out the spawn timeout
        if not self._port_is_free(self._settings.child_port):
            raise OrcError(
                503,
                f"Port {self._settings.child_port} is already in use. "
                "Cannot spawn llama-server.",
                error_type="port_in_use",
            )

        cmd = [
            self._settings.llama_server_bin,
            "--model", profile.model_path,
            "--port", str(self._settings.child_port),
            "--host", "127.0.0.1",
        ] + build_cli_args(profile.flags)

        if profile.chat_template:
            cmd += ["--chat-template", profile.chat_template]

        log.info("Spawning llama-server for model %r: %s", model_id, " ".join(cmd))
        self._state = ChildState.LOADING

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # No creationflags on Windows: avoids severing the stderr pipe.
        )

        child = ChildInfo(model_id=model_id, process=process)
        self._child = child

        # Start reading stderr immediately — before health polling — so we
        # capture all messages emitted during the loading phase.
        child._stderr_reader_task = asyncio.create_task(
            self._read_stderr_loop(child),
            name=f"stderr-{model_id}-{process.pid}",
        )

        ready = await self._health_poll(self._settings.spawn_timeout_seconds)
        if not ready:
            stderr_snapshot = self.get_stderr_tail(50)
            log.error("Model %r failed to become ready. Last stderr:\n%s", model_id, "\n".join(stderr_snapshot))
            await self._kill_and_wait()
            raise OrcError(
                503,
                f"Model {model_id!r} failed to become ready within "
                f"{self._settings.spawn_timeout_seconds}s.",
                error_type="spawn_timeout",
                stderr_tail=stderr_snapshot,
            )

        self._state = ChildState.READY
        log.info("Model %r is ready (pid=%d)", model_id, process.pid)

    async def _kill_and_wait(self) -> None:
        """Terminate the child process and wait for it to exit, then sleep
        for post_kill_delay_seconds to allow VRAM to be released on Windows."""
        if self._child is None:
            self._state = ChildState.IDLE
            return

        self._state = ChildState.DYING
        proc = self._child.process

        if proc.returncode is None:
            log.info("Terminating child pid=%d", proc.pid)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("Child did not exit after terminate(); sending kill()")
                proc.kill()
                await proc.wait()

        log.debug("Child exited with returncode=%s", proc.returncode)

        # Cancel and drain the stderr reader task
        task = self._child._stderr_reader_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._child = None

        log.debug("Waiting %.1fs for VRAM release", self._settings.post_kill_delay_seconds)
        await asyncio.sleep(self._settings.post_kill_delay_seconds)
        self._state = ChildState.IDLE

    async def _health_poll(self, timeout: float) -> bool:
        """Poll the child's /health endpoint until it returns 200 or timeout elapses."""
        url = f"http://127.0.0.1:{self._settings.child_port}/health"
        deadline = asyncio.get_event_loop().time() + timeout

        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                # Fast-fail if the process crashed before binding
                if self._child and self._child.process.returncode is not None:
                    log.warning("Child exited during health poll (rc=%s)", self._child.process.returncode)
                    return False
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return True
                    # 503 = still loading weights — keep polling
                except (httpx.ConnectError, httpx.ReadError, OSError):
                    pass  # server not yet listening
                await asyncio.sleep(0.5)

        return False

    async def _read_stderr_loop(self, child: ChildInfo) -> None:
        """Background task: read child stderr lines into the rolling deque.
        Uses async-for so EOF and task cancellation are handled cleanly.
        errors='replace' tolerates mixed UTF-8/Windows-1252 from llama.cpp builds.
        """
        try:
            async for line_bytes in child.process.stderr:
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                child.stderr_tail.append(line)
                log.debug("[child stderr] %s", line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Stderr reader exited unexpectedly: %s", exc)

    async def _idle_reaper_loop(self) -> None:
        """Background task: evict the loaded model after IDLE_TTL_SECONDS of inactivity."""
        while True:
            await asyncio.sleep(30)
            if (
                self._state == ChildState.READY
                and self._last_used_at is not None
                and (time.monotonic() - self._last_used_at) > self._settings.idle_ttl_seconds
            ):
                model_id = self._child.model_id if self._child else "unknown"
                log.info(
                    "Model %r idle for >%ds — unloading",
                    model_id,
                    self._settings.idle_ttl_seconds,
                )
                await self.kill_current()

    def _is_child_alive(self) -> bool:
        return self._child is not None and self._child.process.returncode is None

    def _port_is_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            return s.connect_ex(("127.0.0.1", port)) != 0
