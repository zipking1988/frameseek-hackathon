"""Exercise the compiler with isolated fake media and a synthetic extractor."""

import hashlib
import json

import pytest

from frameseek.knowledge_compiler import compile_pending
from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge


def setup_store(tmp_path):
    (tmp_path / "original").write_bytes(b"fixture video")
    (tmp_path / "frames").mkdir()
    state = VideoKnowledge(
        video="vid_fixture",
        generation=1,
        duration=16,
        source_fingerprint=hashlib.sha256(b"fixture video").hexdigest(),
        processor_revision="fixture-v1",
        extraction_config="fixture-8",
        scene_boundaries=[4.0],
    )
    return KnowledgeStore(tmp_path, state)


def decode(directory, video, metadata, window, count, prefix):
    frames = []
    for i in range(2):
        file = f"{prefix}_{i}.jpg"
        (directory / "frames" / file).write_bytes(b"fixture image")
        frames.append(
            {
                "t": window["start"] + i,
                "frame_id": f"{video}_{prefix}_{i}",
                "file": file,
                "uri": f"/media/{video}/frames/{file}",
            }
        )
    return frames


def infer(prompt, paths, schema):
    rows = []
    for i, _ in enumerate(paths):
        rows.append(
            {
                "index": str(i),
                "description": "synthetic frame",
                "subjects": [
                    {
                        "id": "one",
                        "facts": [{"field": "kind", "value": "person"}],
                    }
                ],
            }
        )
    return {"text": json.dumps({"frames": rows})}


def test_compile_all_windows_and_reuse_without_calls(tmp_path):
    store = setup_store(tmp_path)
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return infer(*args, **kwargs)

    version = compile_pending(store, {"duration": 16}, counted, lambda: True, decode=decode)
    assert len(calls) == 2
    assert store.read_version(version).pending() == []
    index = store.read_version(version).publishable_index()
    assert not index.catalog_complete
    assert index.frames[2].cut_before  # The scene cut lies between two sampled windows.
    assert compile_pending(store, {"duration": 16}, counted, lambda: True, decode=decode) == version
    assert len(calls) == 2


def test_new_bound_attribute_prompt_is_versioned_and_old_prompt_still_resumes(tmp_path):
    from frameseek.knowledge_compiler import PROMPT, PROMPT_BOUND_ATTRIBUTES, PROMPT_BOUND_OBJECTS

    for name, config, expected in (
        ("old", "knowledge-v3-8sec-8frames-4096tokens", PROMPT),
        ("new", "knowledge-v4-bound-8sec-8frames-4096tokens", PROMPT_BOUND_ATTRIBUTES),
        ("objects", "knowledge-v5-objects-8sec-8frames-4096tokens", PROMPT_BOUND_OBJECTS),
    ):
        directory = tmp_path / name
        directory.mkdir()
        base = setup_store(directory)
        state = base.identity.model_copy(update={"extraction_config": config})
        store = KnowledgeStore(directory, state)
        seen = []

        def capture(prompt, paths, schema, captured=seen):
            captured.append(prompt)
            return infer(prompt, paths, schema)

        compile_pending(store, {"duration": 16}, capture, lambda: True, decode=decode)
        assert seen == [expected, expected]
    assert "garment=blue jersey" in PROMPT_BOUND_ATTRIBUTES
    assert "include its color" in PROMPT_BOUND_OBJECTS


def test_unknown_dispatch_is_never_repeated(tmp_path):
    store = setup_store(tmp_path)
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("unknown response")

    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_pending(store, {"duration": 16}, interrupted, lambda: True, decode=decode)
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_pending(store, {"duration": 16}, interrupted, lambda: True, decode=decode)
    assert len(calls) == 1


def test_budget_error_after_dispatch_marker_does_not_look_retryable(tmp_path):
    from frameseek.attempts import BudgetDenied

    store = setup_store(tmp_path)
    calls = []

    def budget_changed(*args, **kwargs):
        calls.append(1)
        raise BudgetDenied("budget changed while waiting")

    for _ in range(2):
        with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
            compile_pending(store, {"duration": 16}, budget_changed, lambda: True, decode=decode)
    assert len(calls) == 1


def test_cancel_after_response_resumes_without_repeating_that_call(tmp_path):
    store = setup_store(tmp_path)
    running = True
    calls = []

    def cancelled(*args, **kwargs):
        nonlocal running
        calls.append(1)
        running = False
        return infer(*args, **kwargs)

    with pytest.raises(ValueError, match="knowledge_cancelled"):
        compile_pending(store, {"duration": 16}, cancelled, lambda: running, decode=decode)
    assert store.load().pending() == [0, 1]
    version = compile_pending(store, {"duration": 16}, infer, lambda: True, decode=decode)
    assert store.read_version(version).pending() == []
    assert len(calls) == 1


def test_cross_window_sampling_filters_close_frames_before_inference(tmp_path):
    store = setup_store(tmp_path)
    batch_sizes = []

    def shifted_decode(directory, video, metadata, window, count, prefix):
        rows = decode(directory, video, metadata, window, count, prefix)
        if window["start"] == 0:
            rows = rows[:1]
            rows[0]["t"] = 7.5
        return rows

    def counted(prompt, paths, schema):
        batch_sizes.append(len(paths))
        return infer(prompt, paths, schema)

    version = compile_pending(store, {"duration": 16}, counted, lambda: True, decode=shifted_decode)
    times = [frame.t for frame in store.read_version(version).publishable_index().frames]
    assert times == [7.5, 9.0] and batch_sizes == [1, 1]


def test_legacy_close_frame_receipt_is_not_replayed_or_silently_modified(tmp_path):
    from frameseek.knowledge_compiler import packet_to_segment
    from frameseek.occurrences import fingerprint

    store = setup_store(tmp_path)
    state = store.load()
    first = decode(store.directory, state.video, {}, {"start": 0}, 8, "first")[:1]
    first[0]["t"] = 7.5
    state = store.checkpoint(
        packet_to_segment(state, 0, first, infer("fixture", [None], None)),
        fingerprint(state.model_dump(mode="json")),
    )
    second = decode(store.directory, state.video, {}, {"start": 8}, 8, "second")
    (store.base / "window-0001.decoded.json").write_text(json.dumps(second))
    (store.base / "window-0001.raw.json").write_text(json.dumps(infer("fixture", [None] * 2, None)))
    with pytest.raises(ValueError, match="knowledge_sampling_rate_exceeded"):
        compile_pending(
            store,
            {"duration": 16},
            lambda *_a, **_k: pytest.fail("must not dispatch"),
            lambda: True,
            decode=decode,
        )
    assert store.load() == state


def test_short_final_window_without_an_eligible_frame_is_not_sent_or_published(tmp_path):
    store = setup_store(tmp_path)
    store.identity.duration = 8.4
    # Use a fresh store so its cache key matches the modified duration.
    store = KnowledgeStore(tmp_path, store.identity)
    calls = []

    def sparse_decode(directory, video, metadata, window, count, prefix):
        rows = decode(directory, video, metadata, window, count, prefix)[:1]
        rows[0]["t"] = 7.5 if window["start"] == 0 else 8.0
        return rows

    def counted(*args, **kwargs):
        calls.append(1)
        return infer(*args, **kwargs)

    with pytest.raises(ValueError, match="knowledge_sampling_gap"):
        compile_pending(store, {"duration": 8.4}, counted, lambda: True, decode=sparse_decode)
    assert calls == [1]
    assert store.load().pending() == [1]
    assert not (store.base / "window-0001.dispatched.json").exists()


def test_short_final_window_preserves_room_when_an_earlier_frame_is_available(tmp_path):
    store = setup_store(tmp_path)
    store.identity.duration = 8.4
    store = KnowledgeStore(tmp_path, store.identity)
    batches = []

    def decode_tail(directory, video, metadata, window, count, prefix):
        rows = decode(directory, video, metadata, window, count, prefix)
        if window["start"] == 0:
            rows[0]["t"], rows[1]["t"] = 6.5, 7.5
            return rows
        rows = rows[:1]
        rows[0]["t"] = 8.0
        return rows

    def counted(prompt, paths, schema):
        batches.append(len(paths))
        return infer(prompt, paths, schema)

    version = compile_pending(store, {"duration": 8.4}, counted, lambda: True, decode=decode_tail)
    assert [frame.t for frame in store.read_version(version).publishable_index().frames] == [
        6.5,
        8.0,
    ]
    assert batches == [1, 1]


def test_malformed_decoded_frame_is_rejected_before_dispatch(tmp_path):
    store = setup_store(tmp_path)

    def foreign_uri(*args):
        rows = decode(*args)
        rows[0]["uri"] = "/media/other/frames/foreign.jpg"
        return rows

    with pytest.raises(ValueError):
        compile_pending(
            store,
            {"duration": 16},
            lambda *_a, **_k: pytest.fail("must not dispatch"),
            lambda: True,
            decode=foreign_uri,
        )
    assert not (store.base / "window-0000.dispatched.json").exists()
