import pytest

from metrics import MetricsStore
from profiles import BackendEntry, ModelProfile, ProfilesFile


@pytest.fixture
def metrics_store():
    return MetricsStore()


@pytest.fixture
def sample_profiles():
    """A ProfilesFile with one local and one remote profile."""
    return ProfilesFile(
        models={
            "local-model": ModelProfile(
                display_name="Local Model",
                model_path="C:/models/test.gguf",
                estimated_vram_mb=8000,
                flags={"ctx_size": 4096, "n_gpu_layers": 99, "flash_attn": True},
                sampling_defaults={"temperature": 0.2, "top_p": 0.9},
            ),
            "remote-model": ModelProfile(
                display_name="Remote Model",
                backends=[
                    BackendEntry(url="http://10.0.0.1:8090"),
                    BackendEntry(url="http://10.0.0.2:8090"),
                ],
            ),
        }
    )
