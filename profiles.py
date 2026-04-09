from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, model_validator


class BackendEntry(BaseModel):
    """A remote llama-server backend URL for a multi-backend profile."""
    url: str  # e.g. "http://10.0.0.1:8090"


class ModelProfile(BaseModel):
    model_config = {"protected_namespaces": ()}

    display_name: str
    model_path: str = ""           # required when backends is empty
    estimated_vram_mb: int = 0     # used for pre-spawn VRAM sanity check
    flags: dict[str, Any] = {}
    sampling_defaults: dict[str, Any] = {}
    chat_template: str | None = None
    backends: list[BackendEntry] = []   # non-empty → route to remote URLs

    @model_validator(mode="after")
    def check_model_path_or_backends(self) -> "ModelProfile":
        if not self.backends and not self.model_path:
            raise ValueError("model_path is required when no backends are configured")
        return self


class ProfilesFile(BaseModel):
    models: dict[str, ModelProfile]


def load_profiles(path: str) -> ProfilesFile:
    """Load and validate profiles YAML. Raises on parse or validation error (fail fast)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ProfilesFile.model_validate(raw)


def build_cli_args(flags: dict[str, Any]) -> list[str]:
    """Convert a profile flags dict to a flat CLI argument list.

    Rules:
      - key:          replace '_' with '-', prepend '--'
      - value is True  → emit the flag with no value token
      - value is False → skip entirely
      - anything else  → emit '--key' then str(value) as two tokens

    Identity checks (is True / is False) are intentional: 0 is a valid flag value
    and must not be silently dropped.
    """
    args: list[str] = []
    for key, value in flags.items():
        cli_key = "--" + key.replace("_", "-")
        if value is True:
            args.append(cli_key)
        elif value is False:
            continue
        else:
            args.append(cli_key)
            args.append(str(value))
    return args
