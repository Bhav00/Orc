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


settings = Settings()
