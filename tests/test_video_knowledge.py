"""Sampled base-index contracts; synthetic observations, no model calls."""

import hashlib

import pytest

from frameseek.occurrences import fingerprint, search_occurrences
from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge, load_published_knowledge


def empty():
    return VideoKnowledge(
        video="vid_fixture",
        generation=2,
        duration=9.0,
        source_fingerprint="a" * 64,
        processor_revision="fixture-v1",
        extraction_config="sample-8",
    )


def segment(ordinal):
    start = ordinal * 8
    return {
        "ordinal": ordinal,
        "start": float(start),
        "end": float(min(start + 8, 9)),
        "frames": [
            {
                "frame_id": f"vid_fixture_f{ordinal}",
                "ordinal": ordinal,
                "t": float(start),
                "file": f"f{ordinal}.jpg",
                "uri": f"/media/vid_fixture/frames/f{ordinal}.jpg",
                "caption": "fixture",
            }
        ],
        "observations": [
            {
                "frame_id": f"vid_fixture_f{ordinal}",
                "subjects": [
                    {"id": "one", "facts": {"kind": ["person"], "hat": ["red"]}},
                    {"id": "two", "facts": {"kind": ["person"], "shirt": ["white"]}},
                ],
            }
        ],
    }


def test_checkpoints_resume_and_publication_requires_all_segments():
    knowledge = empty().checkpoint(segment(0))
    assert "supplements" not in knowledge.model_dump(mode="json")
    assert knowledge.pending() == [1]
    with pytest.raises(ValueError, match="knowledge_index_incomplete"):
        knowledge.publishable_index()
    reloaded = VideoKnowledge.model_validate_json(knowledge.model_dump_json())
    assert reloaded.checkpoint(segment(0)) == reloaded
    assert reloaded.checkpoint(segment(1)).pending() == []
    index = reloaded.checkpoint(segment(1)).publishable_index()
    assert index.catalog_complete is False
    assert all(frame.end is None for frame in index.frames)


def test_segment_conflicts_and_unbound_observations_are_rejected():
    knowledge = empty().checkpoint(segment(0))
    altered = segment(0)
    altered["frames"][0]["caption"] = "changed"
    with pytest.raises(ValueError, match="knowledge_checkpoint_conflict"):
        knowledge.checkpoint(altered)
    bad = segment(1)
    bad["observations"][0]["frame_id"] = "vid_fixture_other"
    with pytest.raises(ValueError, match="incomplete_knowledge_segment"):
        knowledge.checkpoint(bad)


def test_extractor_cannot_claim_tracking_or_oversample():
    bad = segment(0)
    bad["observations"][0]["subjects"][0]["entity_id"] = "invented_identity"
    with pytest.raises(ValueError, match="knowledge_identity_not_supported"):
        empty().checkpoint(bad)
    bad = segment(0)
    frame = dict(
        bad["frames"][0],
        frame_id="vid_fixture_other",
        ordinal=2,
        t=0.5,
        file="other.jpg",
        uri="/media/vid_fixture/frames/other.jpg",
    )
    bad["frames"].append(frame)
    bad["observations"].append({"frame_id": frame["frame_id"], "subjects": []})
    with pytest.raises(ValueError, match="knowledge_sampling_rate_exceeded"):
        empty().checkpoint(bad)


def test_publishing_checks_sampling_between_windows_but_legacy_versions_remain_readable(tmp_path):
    (tmp_path / "original").write_bytes(b"private fixture")
    (tmp_path / "frames").mkdir()
    for ordinal in range(2):
        (tmp_path / "frames" / f"f{ordinal}.jpg").write_bytes(b"fixture")
    identity = empty().model_copy(
        update={"source_fingerprint": hashlib.sha256(b"private fixture").hexdigest()}
    )
    store = KnowledgeStore(tmp_path, identity)
    first = segment(0)
    first["frames"][0]["t"] = 7.5
    state = store.checkpoint(first, fingerprint(identity.model_dump(mode="json")))
    state = store.checkpoint(segment(1), fingerprint(state.model_dump(mode="json")))
    version = fingerprint(state.model_dump(mode="json"))
    with pytest.raises(ValueError, match="knowledge_sampling_rate_exceeded"):
        store.publish(version)
    # A previously published historical version remains readable.
    (store.base / f"version-{version}.json").write_text(state.model_dump_json())
    assert store.read_version(version) == state


def test_attributes_from_different_subjects_do_not_combine():
    index = empty().checkpoint(segment(0)).checkpoint(segment(1)).publishable_index()
    result = search_occurrences(
        index, [{"field": "hat", "value": "red"}, {"field": "shirt", "value": "white"}]
    )
    assert result["occurrences"] == []
    assert result["coverage"]["all_source_frames_reviewed"] is False


def test_cache_key_changes_with_source_generation_and_processor():
    base = empty()
    for field, value in (
        ("generation", 3),
        ("source_fingerprint", "b" * 64),
        ("processor_revision", "fixture-v2"),
        ("extraction_config", "other"),
    ):
        assert base.model_copy(update={field: value}).cache_key() != base.cache_key()


def test_durable_resume_cas_and_complete_atomic_publication(tmp_path):
    (tmp_path / "original").write_bytes(b"private fixture")
    (tmp_path / "frames").mkdir()
    for ordinal in range(2):
        (tmp_path / "frames" / f"f{ordinal}.jpg").write_bytes(b"fixture")
    identity = empty().model_copy(
        update={"source_fingerprint": hashlib.sha256(b"private fixture").hexdigest()}
    )
    store = KnowledgeStore(tmp_path, identity)
    snapshot = fingerprint(identity.model_dump(mode="json"))
    state = store.checkpoint(segment(0), snapshot)
    with pytest.raises(ValueError, match="knowledge_revision_conflict"):
        store.checkpoint(segment(1), snapshot)
    with pytest.raises(ValueError, match="knowledge_index_incomplete"):
        store.publish(fingerprint(state.model_dump(mode="json")))
    resumed = KnowledgeStore(tmp_path, identity).load()
    assert resumed.pending() == [1]
    state = store.checkpoint(segment(1), fingerprint(resumed.model_dump(mode="json")))
    version = store.publish(fingerprint(state.model_dump(mode="json")))
    assert store.read_version(version) == state
    assert store.publish(fingerprint(state.model_dump(mode="json"))) == version


def test_publish_rejects_changed_source_and_missing_frames(tmp_path):
    (tmp_path / "original").write_bytes(b"source")
    (tmp_path / "frames").mkdir()
    identity = empty().model_copy(
        update={"source_fingerprint": hashlib.sha256(b"source").hexdigest()}
    )
    store = KnowledgeStore(tmp_path, identity)
    state = store.checkpoint(segment(0), fingerprint(identity.model_dump(mode="json")))
    state = store.checkpoint(segment(1), fingerprint(state.model_dump(mode="json")))
    snapshot = fingerprint(state.model_dump(mode="json"))
    with pytest.raises(ValueError, match="knowledge_evidence_missing"):
        store.publish(snapshot)
    (tmp_path / "original").write_bytes(b"changed")
    with pytest.raises(ValueError, match="knowledge_source_changed"):
        store.publish(snapshot)


def test_supplement_creates_new_immutable_version_without_joining_subjects(tmp_path):
    (tmp_path / "original").write_bytes(b"private fixture")
    (tmp_path / "frames").mkdir()
    for ordinal in range(2):
        (tmp_path / "frames" / f"f{ordinal}.jpg").write_bytes(b"fixture")
    identity = empty().model_copy(
        update={"source_fingerprint": hashlib.sha256(b"private fixture").hexdigest()}
    )
    store = KnowledgeStore(tmp_path, identity)
    state = store.checkpoint(segment(0), fingerprint(identity.model_dump(mode="json")))
    state = store.checkpoint(segment(1), fingerprint(state.model_dump(mode="json")))
    original = store.publish(fingerprint(state.model_dump(mode="json")))
    criteria = [{"field": "hat", "value": "red"}, {"field": "shirt", "value": "white"}]
    assert not search_occurrences(store.read_version(original).publishable_index(), criteria)[
        "occurrences"
    ]
    addition = {
        "request_fingerprint": "b" * 64,
        "source_version": original,
        "processor_revision": "fixture-supplement-v1",
        "window_ordinals": [0],
        "observations": [
            {
                "frame_id": "vid_fixture_f0",
                "subjects": [{"id": "independent", "facts": {"hat": ["red"], "shirt": ["white"]}}],
            }
        ],
    }
    revised = store.supplement(addition, original)
    version = store.publish(fingerprint(revised.model_dump(mode="json")))
    assert version != original
    revised_index = load_published_knowledge(
        tmp_path,
        identity.video,
        identity.generation,
        store.key,
        version,
        identity.source_fingerprint,
    )
    assert len(search_occurrences(revised_index, criteria)["occurrences"]) == 1
    assert not search_occurrences(store.read_version(original).publishable_index(), criteria)[
        "occurrences"
    ]
    with pytest.raises(ValueError, match="supplement_already_recorded"):
        store.supplement(addition | {"source_version": version}, version)


def test_supplement_rejects_foreign_frames_and_stale_versions():
    state = empty().checkpoint(segment(0)).checkpoint(segment(1))
    version = fingerprint(state.model_dump(mode="json"))
    item = {
        "request_fingerprint": "c" * 64,
        "source_version": version,
        "processor_revision": "fixture",
        "window_ordinals": [0],
        "observations": [{"frame_id": "vid_fixture_f1", "subjects": []}],
    }
    with pytest.raises(ValueError, match="foreign_supplement_frame"):
        state.supplement(item, version)
    with pytest.raises(ValueError, match="knowledge_revision_conflict"):
        state.supplement(item, "d" * 64)


def test_partial_supplement_does_not_merge_with_original_subject():
    state = empty().checkpoint(segment(0)).checkpoint(segment(1))
    version = fingerprint(state.model_dump(mode="json"))
    item = {
        "request_fingerprint": "d" * 64,
        "source_version": version,
        "processor_revision": "fixture",
        "window_ordinals": [0],
        "observations": [
            {
                "frame_id": "vid_fixture_f0",
                "subjects": [{"id": "two", "facts": {"hat": ["red"]}}],
            }
        ],
    }
    revised = state.supplement(item, version)
    criteria = [{"field": "hat", "value": "red"}, {"field": "shirt", "value": "white"}]
    assert not search_occurrences(revised.publishable_index(), criteria)["occurrences"]
