import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "video_tool", Path(__file__).resolve().parents[1] / "scripts/render_hackathon_video.py"
)
video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video)


def test_narration_is_bounded_before_any_network_call():
    assert video.script_text([{"label": "Demo", "narration": "A real screen."}]) == "A real screen."
    with pytest.raises(ValueError, match="ceiling"):
        video.script_text([{"label": "Demo", "narration": "x" * 3601}])


def test_env_is_data_not_shell_and_does_not_expose_invalid_key(tmp_path):
    path = tmp_path / "voice.env"
    path.write_text("ELEVENLABS_API_KEY=$(do-not-run-this)\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid characters") as error:
        video.load_voice_settings(path)
    assert "do-not-run-this" not in str(error.value)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        video.load_voice_settings(path)


def test_unknown_voice_attempt_is_not_resent(tmp_path, monkeypatch):
    import hashlib
    import json

    import httpx

    path = tmp_path / "voice.env"
    path.write_text("ELEVENLABS_API_KEY=test-key-1234567890\n")
    path.chmod(0o600)
    text = "Hello."
    identity = {
        "voice_id": video.VOICE_ID,
        **video.narration_request(text, video.VOICE_ID, video.MODEL_ID),
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = tmp_path / "voice-cache"
    cache.mkdir()
    (cache / f"{fingerprint}.receipt.json").write_text('{"state":"dispatched"}')
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("Must not send again"))
    with pytest.raises(ValueError, match="will not be sent again"):
        video.generate_voice(text, path, tmp_path)


def test_alignment_rejects_fabricated_or_overlong_timings():
    alignment = {
        "characters": list("Hi"),
        "character_start_times_seconds": [0, 0.1],
        "character_end_times_seconds": [0.1, 0.2],
    }
    assert video.timing_arrays(alignment, "Hi")[1][-1] == 0.2
    with pytest.raises(ValueError, match="match"):
        video.timing_arrays(alignment, "Bye")
    alignment["character_end_times_seconds"][-1] = 180
    with pytest.raises(ValueError, match="limit"):
        video.timing_arrays(alignment, "Hi")
