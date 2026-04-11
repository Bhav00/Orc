import pytest
from pydantic import ValidationError

from profiles import BackendEntry, ModelProfile, ProfilesFile, build_cli_args, load_profiles


# ---------------------------------------------------------------------------
# build_cli_args
# ---------------------------------------------------------------------------

class TestBuildCliArgs:
    def test_boolean_true_emits_flag_only(self):
        assert build_cli_args({"flash_attn": True}) == ["--flash-attn"]

    def test_boolean_false_omitted(self):
        assert build_cli_args({"flash_attn": False}) == []

    def test_zero_is_valid_value_not_dropped(self):
        """Invariant #3: identity checks — 0 must not be silently dropped."""
        assert build_cli_args({"parallel": 0}) == ["--parallel", "0"]

    def test_integer_value(self):
        assert build_cli_args({"ctx_size": 8192}) == ["--ctx-size", "8192"]

    def test_string_value(self):
        assert build_cli_args({"cache_type_k": "q8_0"}) == ["--cache-type-k", "q8_0"]

    def test_underscore_to_dash(self):
        assert build_cli_args({"n_gpu_layers": 99}) == ["--n-gpu-layers", "99"]

    def test_empty_dict(self):
        assert build_cli_args({}) == []

    def test_mixed_flags(self):
        result = build_cli_args({
            "flash_attn": True,
            "mlock": False,
            "parallel": 0,
            "ctx_size": 4096,
            "cache_type_k": "q8_0",
        })
        assert "--flash-attn" in result
        assert "--mlock" not in result
        assert result[result.index("--parallel") + 1] == "0"
        assert result[result.index("--ctx-size") + 1] == "4096"
        assert result[result.index("--cache-type-k") + 1] == "q8_0"


# ---------------------------------------------------------------------------
# ModelProfile validation
# ---------------------------------------------------------------------------

class TestModelProfileValidation:
    def test_valid_with_model_path(self):
        p = ModelProfile(display_name="Test", model_path="/path/to/model.gguf")
        assert p.model_path == "/path/to/model.gguf"
        assert p.backends == []

    def test_valid_with_backends(self):
        p = ModelProfile(
            display_name="Test",
            backends=[BackendEntry(url="http://10.0.0.1:8090")],
        )
        assert len(p.backends) == 1
        assert p.model_path == ""

    def test_neither_model_path_nor_backends_raises(self):
        with pytest.raises(ValidationError, match="model_path is required"):
            ModelProfile(display_name="Test")

    def test_sampling_defaults_optional(self):
        p = ModelProfile(display_name="Test", model_path="/m.gguf")
        assert p.sampling_defaults == {}


# ---------------------------------------------------------------------------
# load_profiles
# ---------------------------------------------------------------------------

class TestLoadProfiles:
    def test_valid_yaml(self, tmp_path):
        yaml_content = """
models:
  test-model:
    display_name: "Test"
    model_path: "/path/to/model.gguf"
    estimated_vram_mb: 8000
    flags:
      ctx_size: 4096
"""
        path = tmp_path / "profiles.yaml"
        path.write_text(yaml_content)
        pf = load_profiles(str(path))
        assert "test-model" in pf.models
        assert pf.models["test-model"].flags["ctx_size"] == 4096

    def test_invalid_yaml_raises(self, tmp_path):
        path = tmp_path / "profiles.yaml"
        path.write_text("models:\n  bad:\n    display_name: 123\n")
        with pytest.raises(Exception):
            load_profiles(str(path))

    def test_missing_file_raises(self):
        with pytest.raises(Exception):
            load_profiles("/nonexistent/profiles.yaml")
