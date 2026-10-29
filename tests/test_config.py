import pytest

from frameseek.config import ConfigurationError, load_settings


def test_environment_over_file_over_defaults(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text("[frameseek]\ncoarse_frames=8\ncandidate_k=12\n")
    settings = load_settings(config, {"FRAMESEEK_COARSE_FRAMES": "16"})
    assert settings.coarse_frames == 16
    assert settings.candidate_k == 12
    assert settings.zoom_cap == 3


@pytest.mark.parametrize(
    "key,value",
    [
        ("PRIMARY_CAP", "32"),
        ("ZOOM_CAP", "4"),
        ("MAX_WINDOW_SECONDS", "121"),
        ("MAX_FPS", "2"),
        ("MAX_FRAMES", "129"),
        ("COARSE_RETRY_CAP", "1"),
        ("TEXT_REPAIR_CAP", "2"),
        ("COARSE_FRAMES", "17"),
        ("TTL_HOURS", "0"),
        ("SCENE_THRESHOLD", "NaN"),
        ("MAX_FPS", "true"),
        ("RERANK_K", "11"),
    ],
)
def test_protected_and_invalid_configuration_is_rejected(key, value):
    with pytest.raises(ConfigurationError):
        load_settings(environ={"FRAMESEEK_" + key: value})


def test_configuration_never_echoes_secret(tmp_path):
    config = tmp_path / "settings.toml"
    secret = "test-secret-do-not-log"
    config.write_text(f'[frameseek]\napi_key="{secret}"\n')
    with pytest.raises(ConfigurationError) as exc:
        load_settings(config, {})
    assert secret not in str(exc.value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings(environ={"FRAMESEEK_BUDGET_APPROVED": secret})
    assert secret not in str(exc.value)


def test_invalid_or_unreadable_file_has_safe_error(tmp_path):
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path / "missing", {})
    config = tmp_path / "settings.toml"
    config.write_text("[frameseek\n")
    with pytest.raises(ConfigurationError):
        load_settings(config, {})
