"""Synthetic scoring inputs do not imply recognition-quality acceptance."""

import pytest

from frameseek.knowledge_evaluation import (
    REQUIRED_CATEGORIES,
    catalog_from_index,
    prepare_review_case,
    sampled_index_predictions,
    score_cases,
)


def case(i):
    kind = "real" if i % 2 else "synthetic"
    return {
        "id": f"case-{i}",
        "source_kind": kind,
        "source_id": kind,
        "source_fingerprint": "a" * 64,
        "index_version": "b" * 64,
        "question": f"fixture question {i}",
        "scope_start": 0,
        "scope_end": 2,
        "reviewed": True,
        "reviewer": "test-only",
        "visible_duration_seconds": 1,
        "categories": sorted(REQUIRED_CATEGORIES),
        "followup_check": {
            "reviewed": True,
            "parent_request_id": "parent",
            "resolved_question": "fixture resolved question",
            "evidence_reference": "fixture page recording",
            "source_fingerprint": "a" * 64,
            "index_version": "b" * 64,
        },
        "seek_checks": [
            {
                "reviewed": True,
                "frame_id": "f1",
                "target_time": 0,
                "player_time": 0,
                "evidence_reference": "fixture page recording",
            }
        ],
        "sampled_frames": ["f1", "f2"],
        "frame_labels": [
            {"frame_id": "f1", "visible": True},
            {"frame_id": "f2", "visible": False},
        ],
        "expected": ["f1"],
        "predicted": ["f1"],
    }


def catalog():
    return {
        kind: {
            "source_kind": kind,
            "source_fingerprint": "a" * 64,
            "index_version": "b" * 64,
            "duration": 2,
            "frames": [{"id": "f1", "t": 0}, {"id": "f2", "t": 1}],
        }
        for kind in ("real", "synthetic")
    }


def test_separate_metrics_and_thresholds():
    result = score_cases([case(i) for i in range(20)], source_catalog=catalog())
    assert result["accepted"] and result["precision"] == result["recall"] == 1
    assert result["groups"]["real"]["cases"] == result["groups"]["synthetic"]["cases"] == 10
    cases = [case(i) for i in range(20)]
    for item in cases[:4]:
        item["predicted"] = []
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"] and result["recall"] == 0.8


def test_unreviewed_or_unlabelled_predictions_cannot_pass():
    cases = [case(i) for i in range(20)]
    cases[0]["reviewed"] = False
    cases[1]["predicted"] = ["unreviewed-frame"]
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"] and len(result["issues"]) == 2
    with pytest.raises(ValueError, match="duplicate_or_missing"):
        score_cases([case(1), case(1)], source_catalog=catalog())


def test_missing_real_cases_or_required_categories_cannot_pass():
    cases = [
        case(i) | {"source_kind": "synthetic", "source_id": "synthetic", "categories": []}
        for i in range(20)
    ]
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"] and result["groups"]["real"]["cases"] == 0
    assert set(result["missing_categories"]) == REQUIRED_CATEGORIES


def test_missing_catalog_duplicate_case_and_omitted_scoped_frame_cannot_pass():
    cases = [case(i) for i in range(20)]
    assert not score_cases(cases)["accepted"]
    cases[2]["question"] = cases[0]["question"]
    cases[4]["sampled_frames"] = ["f1"]
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"]
    assert "case-2:duplicate_case" in result["issues"]
    assert "case-4:incomplete_frame_labels" in result["issues"]


def test_version_mismatch_and_unordered_source_catalog_are_rejected():
    cases = [case(i) for i in range(20)]
    cases[0]["index_version"] = "c" * 64
    assert (
        "case-0:source_or_scope_invalid" in score_cases(cases, source_catalog=catalog())["issues"]
    )
    sources = catalog()
    sources["synthetic"]["frames"].reverse()
    with pytest.raises(ValueError, match="invalid_evaluation_catalog"):
        score_cases([case(0)], source_catalog=sources)


def test_review_catalog_uses_actual_index_timestamps_and_identity():
    from test_occurrences import catalog as index_fixture

    index = index_fixture(2)
    version = "c" * 64
    source = catalog_from_index(index, index_version=version, source_kind="synthetic")[index.video]
    assert source["source_fingerprint"] == index.source_fingerprint
    assert source["index_version"] == version
    assert source["frames"] == [{"id": frame.frame_id, "t": frame.t} for frame in index.frames]
    with pytest.raises(ValueError, match="invalid_evaluation_identity"):
        catalog_from_index(index, index_version="not-a-version", source_kind="synthetic")


def test_followup_and_seek_require_reviewed_context_and_actual_seek():
    cases = [case(i) for i in range(20)]
    cases[0].pop("followup_check")
    cases[1]["followup_check"]["index_version"] = "c" * 64
    cases[2]["seek_checks"][0]["player_time"] = 1
    cases[3]["seek_checks"][0]["frame_id"] = "unknown"
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"]
    assert {
        "case-0:followup_unverified",
        "case-1:followup_unverified",
        "case-2:seek_unverified",
        "case-3:seek_unverified",
    } <= set(result["issues"])


def test_seek_must_replay_a_returned_evidence_frame():
    cases = [case(i) for i in range(20)]
    # A correctly timed player seek to a sampled frame is insufficient if the
    # answer never cited it. It does not test the evidence-to-player flow.
    cases[0]["seek_checks"][0].update(frame_id="f2", target_time=1, player_time=1)
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"]
    assert "case-0:seek_unverified" in result["issues"]


def test_every_scoped_frame_requires_an_explicit_consistent_review():
    cases = [case(i) for i in range(20)]
    cases[0].pop("frame_labels")
    cases[1]["frame_labels"].pop()
    cases[2]["frame_labels"][1]["frame_id"] = "f1"
    cases[3]["frame_labels"][1]["visible"] = True
    cases[4]["frame_labels"][1]["visible"] = "uncertain"
    result = score_cases(cases, source_catalog=catalog())
    assert not result["accepted"]
    assert {f"case-{i}:frame_reviews_incomplete" for i in range(5)} <= set(result["issues"])


def test_review_packet_starts_unlabelled_and_contains_only_scoped_source_frames():
    from test_occurrences import catalog as index_fixture

    index = index_fixture(4)
    version = "c" * 64
    kwargs = {
        "index_version": version,
        "source_kind": "synthetic",
        "case_id": "review-1",
        "question": "find red hat",
        "start": 1,
        "end": 3,
        "categories": ["person", "appearance"],
    }
    packet = prepare_review_case(index, **kwargs)
    assert packet["reviewed"] is False and packet["predicted"] is None
    assert packet["sampled_frames"] == ["vid_offline_f1", "vid_offline_f2"]
    assert [item["visible"] for item in packet["frame_labels"]] == [None, None]
    assert [item["t"] for item in packet["frame_references"]] == [1, 2]
    assert not score_cases(
        [packet],
        source_catalog=catalog_from_index(index, index_version=version, source_kind="synthetic"),
    )["accepted"]
    with pytest.raises(ValueError, match="review_scope_has_no_sampled_frames"):
        prepare_review_case(index, **(kwargs | {"start": 0.1, "end": 0.2}))
    with pytest.raises(ValueError, match="invalid_review_case"):
        prepare_review_case(index, **(kwargs | {"categories": ["other"]}))


def test_review_predictions_read_all_sampled_hits_without_model_calls():
    from test_occurrences import catalog as index_fixture
    from test_occurrences import observation

    from frameseek.occurrences import checkpoint, pending_batches

    index = index_fixture(120, complete=False)
    for batch in pending_batches(index):
        rows = [observation(int(frame.rsplit("f", 1)[1])) for frame in batch]
        index = checkpoint(index, rows, index.snapshot())
    args = {
        "index_version": "d" * 64,
        "source_kind": "synthetic",
        "question": "find people",
        "start": 0,
        "end": 120,
    }
    result = sampled_index_predictions(index, **args)
    assert len(result["predicted"]) == 120
    assert result["predicted"][0] == "vid_offline_f0"
    assert result["predicted"][-1] == "vid_offline_f119"
    assert result["index_snapshot"] == index.snapshot() and result["model_requests_sent"] == 0
    with pytest.raises(ValueError, match="review_scope_mismatch"):
        sampled_index_predictions(index, **(args | {"question": "find people from 00:01-00:02"}))
    with pytest.raises(ValueError, match="sampled_review_requires_sampled_index"):
        sampled_index_predictions(index_fixture(2), **(args | {"end": 2}))
    with pytest.raises(ValueError, match="sampled_review_does_not_support_event_counts"):
        sampled_index_predictions(index, **(args | {"question": "how many people"}))
