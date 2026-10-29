"""Synthetic approvals only; no live provider or spending records."""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from frameseek.attempts import ApprovedBudget, AttemptLedger, BudgetDenied
from frameseek.knowledge_budget import ExtractionCalibration, admit_base_index


def fixtures(tmp_path):
    ledger = AttemptLedger(tmp_path / "synthetic-ledger.sqlite3")
    for scope in ("*", "owner"):
        ledger.install_approved_record(
            ApprovedBudget(scope, "fixture", "synthetic test only", 2, 4, 40, "USD_MICRO")
        )
    runtime = SimpleNamespace(
        provider="tokenfactory",
        allow_tokenfactory_media=True,
        vision=SimpleNamespace(revision="fixture-model-v1", max_cost_micro=10),
        currency_unit="USD_MICRO",
        budget_version="fixture",
        valid_until=time.time() + 3600,
        blockers=lambda: [],
    )
    calibration = ExtractionCalibration(
        reference="synthetic calibration only",
        processor_revision="fixture-model-v1",
        contract="video_knowledge_extraction_v3",
        max_frames=8,
        max_output_tokens=4096,
        max_cost_units=8,
        currency_unit="USD_MICRO",
        valid_until=runtime.valid_until - 1,
    )
    return ledger, runtime, calibration


def test_preflight_counts_only_new_batches_and_does_not_dispatch(tmp_path):
    ledger, runtime, calibration = fixtures(tmp_path)
    estimate = admit_base_index(
        ledger, "owner", runtime, calibration, completed_windows=2, total_windows=4
    )
    assert estimate["new_calls"] == 2 and estimate["maximum_cost_units"] == 20
    with ledger._connect() as connection:
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    with pytest.raises(BudgetDenied):
        admit_base_index(
            ledger, "owner", runtime, calibration, completed_windows=0, total_windows=3
        )


def test_even_a_fully_cached_resume_requires_both_active_approvals(tmp_path):
    ledger, runtime, calibration = fixtures(tmp_path)
    assert (
        admit_base_index(
            ledger, "owner", runtime, calibration, completed_windows=2, total_windows=2
        )["new_calls"]
        == 0
    )
    ledger.restrict("*", "fixture")
    with pytest.raises(BudgetDenied):
        admit_base_index(
            ledger, "owner", runtime, calibration, completed_windows=2, total_windows=2
        )


@pytest.mark.parametrize(
    "change",
    [
        {"contract": "older_other_schema"},
        {"max_frames": 7},
        {"max_output_tokens": 4095},
        {"max_cost_units": 11},
        {"processor_revision": "other"},
        {"valid_until": 0},
    ],
)
def test_new_schema_requires_matching_current_calibration(tmp_path, change):
    ledger, runtime, calibration = fixtures(tmp_path)
    with pytest.raises(BudgetDenied):
        admit_base_index(
            ledger,
            "owner",
            runtime,
            replace(calibration, **change),
            completed_windows=0,
            total_windows=1,
        )
