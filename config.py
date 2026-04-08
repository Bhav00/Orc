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


settings = Settings()
