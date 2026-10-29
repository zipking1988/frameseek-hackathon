import copy

import pytest
from pydantic import ValidationError

from frameseek.event_evaluation import EventBenchmark, EventRun, score_events


def artifacts():
    benchmark = {
        "sources": [
            {
                "id": "clip",
                "sha256": "a" * 64,
                "duration": 30.0,
                "split": "development",
                "group": "game-A",
                "reviewed": [{"start": 0.0, "end": 20.0}],
            }
        ],
        "labels": [
            {
                "id": "one",
                "source": "clip",
                "label": "lift",
                "canonical": "one",
                "verdict": "confirmed",
                "start": 1.0,
                "end": 3.0,
                "evidence_times": [1.5],
                "basis": "reviewed action",
            },
            {
                "id": "replay",
                "source": "clip",
                "label": "lift",
                "canonical": "one",
                "verdict": "confirmed",
                "start": 6.0,
                "end": 8.0,
                "evidence_times": [7.0],
                "basis": "reviewed replay",
            },
        ],
    }
    run = {
        "extractor": "offline-test",
        "source_hashes": {"clip": "a" * 64},
        "predictions": [
            {k: v for k, v in e.items() if k not in ["verdict", "basis"]}
            for e in benchmark["labels"]
        ],
    }
    return benchmark, run


def score(benchmark, run, threshold=0.5):
    return score_events(
        EventBenchmark.model_validate(benchmark), EventRun.model_validate(run), threshold
    )


def test_matching_replay_clips_count_once_and_do_not_claim_full_coverage():
    b, r = artifacts()
    result = score(b, r)
    assert result["precision"] == result["recall"] == 1
    assert result["reviewed_unique_events"] == result["predicted_unique_events"] == 1
    assert result["count_absolute_error"] == result["incorrect_replay_merges"] == 0
    assert not result["full_source_reviewed"]
    assert result["model_requests_sent"] == 0


def test_duplicate_prediction_cannot_match_same_label_twice():
    b, r = artifacts()
    r["predictions"].append(copy.deepcopy(r["predictions"][0]) | {"id": "duplicate"})
    result = score(b, r)
    assert result["true_positive"] == 2
    assert result["false_positive"] == 1
    assert len(result["false_positive_ids"]) == 1


def test_incorrect_replay_merge_is_detected_even_if_clips_match():
    b, r = artifacts()
    b["labels"][1]["canonical"] = "different-event"
    result = score(b, r)
    assert result["incorrect_replay_merges"] == 1
    assert result["count_absolute_error"] == 1


def test_unreviewed_and_uncertain_predictions_are_not_confirmed_negatives():
    b, r = artifacts()
    b["labels"][1]["verdict"] = "uncertain"
    b["labels"][1]["canonical"] = "ambiguous"
    r["predictions"].append(
        r["predictions"][0]
        | {"id": "outside", "start": 24.0, "end": 26.0, "evidence_times": [25.0]}
    )
    result = score(b, r)
    assert result["uncertain_prediction_ids"] == ["replay"]
    assert result["unreviewed_prediction_ids"] == ["outside"]
    assert result["false_positive"] == 0


def test_wrong_action_or_localization_is_not_credited():
    b, r = artifacts()
    r["predictions"][0]["label"] = "drop"
    r["predictions"][1].update(start=6.0, end=6.2, evidence_times=[6.1])
    result = score(b, r)
    assert result["true_positive"] == 0
    assert result["false_positive"] == result["false_negative"] == 2


def test_empty_run_reports_misses_without_inventing_precision_or_training_verdict():
    b, r = artifacts()
    r["predictions"] = []
    result = score(b, r)
    assert result["precision"] is None
    assert result["recall"] == 0
    assert result["training_decision"] == "not_established_by_event_metrics_alone"


def test_source_hashes_split_leakage_and_out_of_bounds_are_rejected():
    b, r = artifacts()
    r["source_hashes"]["clip"] = "b" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        score(b, r)
    b["sources"].append(b["sources"][0] | {"id": "holdout", "split": "held_out"})
    with pytest.raises(ValidationError, match="leaked"):
        EventBenchmark.model_validate(b)
    b, r = artifacts()
    r["predictions"][0]["evidence_times"] = [31.0]
    with pytest.raises(ValidationError, match="outside prediction"):
        score(b, r)


def test_evaluation_rejects_unknown_sources_and_invalid_thresholds():
    b, r = artifacts()
    r["predictions"][0]["source"] = "foreign"
    with pytest.raises(ValueError, match="outside source"):
        score(b, r)
    with pytest.raises(ValueError, match="threshold"):
        score(*artifacts(), threshold=0)


def test_count_errors_cannot_cancel_across_different_actions():
    b, r = artifacts()
    r["predictions"][0]["label"] = "drop"
    r["predictions"][1]["label"] = "drop"
    result = score(b, r)
    assert result["count_absolute_error"] == 2
    assert len(result["counts"]) == 2


def test_uncertain_overlap_cannot_hide_a_confirmed_detection_or_wrong_label():
    b, r = artifacts()
    b["labels"].append(
        b["labels"][0] | {"id": "uncertain-overlap", "canonical": "maybe", "verdict": "uncertain"}
    )
    assert score(b, r)["true_positive"] == 2
    r["predictions"][0]["label"] = "wrong"
    result = score(b, r)
    assert result["false_positive"] == 1
    assert result["false_negative"] == 1


def test_different_encodes_from_same_game_cannot_leak_into_holdout():
    b, _ = artifacts()
    b["sources"].append(
        b["sources"][0] | {"id": "other-encode", "sha256": "b" * 64, "split": "held_out"}
    )
    with pytest.raises(ValidationError, match="group leaked"):
        EventBenchmark.model_validate(b)
