"""Real FFmpeg regression for absolute-time sampling after a seek."""

import shutil
import subprocess

import pytest

from frameseek.media import frames, probe


def test_seek_window_keeps_all_decoded_absolute_timestamps(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10",
            "-t",
            "20",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=20,
    )
    source.rename(tmp_path / "original")
    metadata = probe(tmp_path)
    sampled = frames(tmp_path, "vid_test", metadata, {"start": 12.0, "end": 18.0}, 6, "late")
    assert len(sampled) == 6
    assert [item["t"] for item in sampled] == pytest.approx([12, 13, 14, 15, 16, 17], abs=0.11)
    assert all((tmp_path / "frames" / item["file"]).is_file() for item in sampled)


def test_variable_rate_short_tail_keeps_valid_sampling_across_windows(tmp_path):
    from test_knowledge_compiler import infer

    from frameseek.index_service import source_digest
    from frameseek.knowledge_compiler import compile_pending
    from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    source = tmp_path / "vfr.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x72:rate=10",
            "-t",
            "8.4",
            "-vf",
            r"select=not(between(t\,7\,7.49))",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=20,
    )
    source.rename(tmp_path / "original")
    metadata = probe(tmp_path)
    state = VideoKnowledge(
        video="vid_vfr",
        generation=1,
        duration=metadata["duration"],
        source_fingerprint=source_digest(tmp_path),
        processor_revision="synthetic-only",
        extraction_config="synthetic-only",
    )
    store = KnowledgeStore(tmp_path, state)
    version = compile_pending(store, metadata, infer, lambda: True)
    timestamps = [frame.t for frame in store.read_version(version).publishable_index().frames]
    assert timestamps == pytest.approx([0, 1, 2, 3, 4, 5, 6, 8], abs=0.01)
    assert all(b - a >= 0.99999 for a, b in zip(timestamps, timestamps[1:], strict=False))


def test_full_source_catalog_enumerates_real_frames_and_stores_no_fake_observations(tmp_path):
    from frameseek.occurrences import pending_batches, plan_source

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    source = tmp_path / "short.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=20,
    )
    source.rename(tmp_path / "original")
    index = plan_source(tmp_path, "vid_short", 1, probe(tmp_path), "offline-plan-only")
    assert len(index.frames) == 20
    assert [f.t for f in index.frames] == pytest.approx([i / 10 for i in range(20)])
    assert index.frames[-1].end == 2
    assert not index.observations
    assert list(map(len, pending_batches(index))) == [8, 8, 4]
    assert not (tmp_path / "frames").exists()
