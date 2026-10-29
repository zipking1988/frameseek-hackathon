import copy

import pytest
from pydantic import ValidationError

from frameseek.evaluation import Dataset, Interval, Run, covers_events, digest, score
from frameseek.fixtures import examples


def artifacts():
    dataset = Dataset.model_validate(
        {
            "id": "test-only",
            "split": "diagnostic",
            "sources": [{"id": "fixture_video", "kind": "synthetic", "sha256": "a" * 64}],
            "cases": [
                {
                    "id": f"{kind}-{i}",
                    "video_id": "fixture_video",
                    "category": kind,
                    "question": "Synthetic scorer regression",
                    "query_language": "en",
                    "annotation_state": "reviewed",
                    "annotated_by": "test-fixture",
                    "events": [] if kind == "no_match" else [{"start": 2.0, "end": 4.0}],
                    "relevant_windows": [] if kind == "no_match" else ["w0"],
                }
                for kind in ("short_visual", "speech", "cross_window", "no_match")
                for i in range(2)
            ],
        }
    )
    results = []
    for case in dataset.cases:
        response = examples()["no_result" if case.category == "no_match" else "ok"].model_dump(
            mode="json"
        )
        results.append(
            {
                "case_id": case.id,
                "outcome": "completed",
                "response": response,
                "raw_response": response,
                "candidate_windows": ["w0"],
                "elapsed_seconds": 1.0,
                "cost_micro": 2,
                "reviewed_response_sha256": digest(response),
                "reviewed_by": "test-fixture",
                "playback_verified": True,
                "claim_inventory_complete": True,
                "claims": []
                if case.category == "no_match"
                else [
                    {
                        "event_index": 0,
                        "statement": response["events"][0]["summary"],
                        "supported": True,
                    }
                ],
            }
        )
    run = Run.model_validate(
        {
            "id": "synthetic-run",
            "mode": "fixture",
            "variant": "full_agent",
            "dataset_sha256": digest(dataset),
            "models": {"vision": "mock@1"},
            "configuration_sha256": "b" * 64,
            "currency_unit": "USD_MICRO",
            "results": results,
        }
    )
    return dataset, run


def test_fixture_metrics_are_not_live_acceptance():
    dataset, run = artifacts()
    report = score(dataset, run)
    assert report["metrics"]["recall_at_5_macro"] == 1
    assert report["metrics"]["iou_all_events_at_0_5_rate"] == 1
    assert report["metrics"]["evidence_accuracy"] == 1
    assert report["diagnostic_gate"]["status"] == "NOT_ACCEPTED"
    assert "fixture_or_synthetic_not_live_acceptance" in report["diagnostic_gate"]["gaps"]


def test_missing_and_failed_cases_stay_in_metric_denominators():
    dataset, run = artifacts()
    run.results.pop(0)
    run.results[0].outcome = "timeout"
    run.results[0].response = None
    run.results[0].candidate_windows = None
    report = score(dataset, run)["metrics"]
    assert report["requests"] == 8
    assert report["outcomes"]["missing"] == 1
    assert report["outcomes"]["timeout"] == 1
    assert report["schema_final_rate"] == 6 / 8
    assert report["recall_at_5_macro"] == 4 / 6
    assert report["iou_all_events_at_0_5_rate"] == 4 / 6
    assert report["cost_total_micro"] is None


def test_missing_retrieval_trace_is_unknown_not_perfect_recall():
    dataset, run = artifacts()
    run.results[0].candidate_windows = None
    metrics = score(dataset, run)["metrics"]
    assert metrics["recall_at_5_macro"] is None
    assert metrics["recall_observed_questions"] == 5


def test_evidence_review_is_bound_to_exact_response_and_complete_claim_inventory():
    dataset, run = artifacts()
    run.results[0].response["answer"] = "Changed after human review"
    report = score(dataset, run)
    assert report["metrics"]["evidence_accuracy"] is None
    assert not report["cases"][0]["playback_verified"]
    dataset, run = artifacts()
    run.results[0].claims[0].supported = None
    assert score(dataset, run)["metrics"]["evidence_accuracy"] is None
    run.results[0].claims[0].supported = False
    assert score(dataset, run)["metrics"]["evidence_accuracy"] == 5 / 6


def test_iou_coverage_requires_distinct_matches_and_keeps_missed_events():
    a = Interval(start=2.0, end=4.0)
    b = Interval(start=3.0, end=5.0)
    broad = Interval(start=2.0, end=5.0)
    assert not covers_events([a, b], [broad])
    assert covers_events([a, b], [broad, b])
    assert not covers_events([a], [Interval(start=4.0, end=5.0)])


def test_negative_false_positive_and_invalid_schema_are_reported_separately():
    dataset, run = artifacts()
    run.results[-1].response = examples()["ok"].model_dump(mode="json")
    run.results[-2].response = {"status": "no_result"}
    metrics = score(dataset, run)["metrics"]
    assert metrics["schema_final_rate"] == 7 / 8
    assert metrics["negative_correct_rate"] == 0
    assert metrics["negative_false_positive_rate"] == 0.5


def test_run_dataset_identity_duplicates_and_source_ownership_are_checked():
    dataset, run = artifacts()
    run.dataset_sha256 = "0" * 64
    with pytest.raises(ValueError, match="frozen"):
        score(dataset, run)
    run.dataset_sha256 = digest(dataset)
    run.results.append(copy.deepcopy(run.results[0]))
    with pytest.raises(ValueError, match="Duplicate"):
        score(dataset, run)
    raw = dataset.model_dump(mode="json")
    raw["cases"][0]["video_id"] = "other"
    with pytest.raises(ValidationError):
        Dataset.model_validate(raw)


def test_latency_completion_coverage_and_unknown_cost_are_explicit():
    dataset, run = artifacts()
    for i, result in enumerate(run.results):
        result.elapsed_seconds = float(i + 1)
    run.results[-1].elapsed_seconds = 1000.0
    run.results[-1].outcome = "timeout"
    run.results[-1].response = None
    run.results[0].cost_micro = None
    metrics = score(dataset, run)["metrics"]
    assert metrics["latency_p95_completed_seconds"] == 7.0
    assert metrics["completion_rate"] == 7 / 8
    assert metrics["cost_total_micro"] is None
    assert metrics["cost_observed_requests"] == 7


def test_wrong_video_answer_and_nonfinite_measurements_cannot_count_as_valid():
    dataset, run = artifacts()
    run.results[0].response["events"][0]["video_id"] = "other"
    assert score(dataset, run)["metrics"]["schema_final_rate"] == 7 / 8
    raw = run.model_dump(mode="json")
    raw["results"][0]["elapsed_seconds"] = float("nan")
    with pytest.raises(ValidationError):
        Run.model_validate(raw)


def test_raw_schema_uses_all_requests_even_when_raw_outputs_are_missing():
    dataset, run = artifacts()
    for result in run.results[1:]:
        result.raw_response = None
    metrics = score(dataset, run)["metrics"]
    assert metrics["schema_raw_rate"] == 1 / 8
    assert metrics["schema_raw_observed"] == 1
    run.results[0].raw_response = None
    assert score(dataset, run)["metrics"]["schema_raw_rate"] is None


def test_failed_outcomes_cannot_count_retained_responses_as_task_success():
    dataset, run = artifacts()
    run.results[0].outcome = "timeout"
    run.results[-1].outcome = "failed"
    metrics = score(dataset, run)["metrics"]
    assert metrics["iou_all_events_at_0_5_rate"] == 5 / 6
    assert metrics["negative_correct_rate"] == 1 / 2
    assert metrics["completion_rate"] == 6 / 8
    assert metrics["evidence_accuracy"] == 1  # retained, verified claims are distinct
