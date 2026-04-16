from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    profiles_path: str = Field("profiles.yaml")
    llama_server_bin: str = Field("llama-server.exe")
    child_port: int = Field(8090)
    host: str = Field("127.0.0.1")
    port: int = Field(8080)
    vram_total_mb: int = Field(32000)
    vram_reserve_mb: int = Field(2000)
    spawn_timeout_seconds: int = Field(60)
    post_kill_delay_seconds: float = Field(2.0)

    # Phase 2 — loaded now so .env.example stays complete, unused in Phase 1
    idle_ttl_seconds: int = Field(600)
    admin_key: str | None = Field(None)

    # Backend health polling interval for remote profiles (seconds); 0 = disabled
    backend_health_interval: int = Field(30)

    # CORS — comma-separated origins, or "*" for all; empty = disabled
    cors_origins: str = Field("")

    # Startup preloading — model ID to pre-load on startup; empty = disabled
    preload_model: str = Field("")

    # Logging
    log_dir: str = Field("logs")

    # Metrics persistence
    # Interval (seconds) between JSON snapshot saves; 0 = disabled (no save/load)
    metrics_snapshot_interval: int = Field(60)

    # Request queuing during model swap
    # Max seconds a request will wait for the spawn lock before returning 503.
    # 0 = wait indefinitely (original behaviour).
    swap_timeout_seconds: int = Field(30)
    # Max number of requests allowed to queue for a model swap at once.
    # When the queue is full, further requests are rejected immediately with 503.
    # 0 = unlimited.
    swap_queue_depth: int = Field(0)

    # Repetition detection — sliding-window character-level detector.
    # Window size in characters (0 = detection disabled entirely).
    repeat_detection_window: int = Field(0)
    # Number of consecutive repeats of the same pattern to trigger detection.
    repeat_detection_threshold: int = Field(4)
    # Action on detection: "abort" = inject error SSE and stop stream;
    # "warn" = log only, let response pass through.
    repeat_detection_action: str = Field("abort")


settings = Settings()
