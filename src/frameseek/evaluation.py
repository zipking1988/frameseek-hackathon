"""Offline, versioned evaluation. Reads artifacts; never invokes a model or grants budget."""

import argparse
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from frameseek.schema import Answer

CATEGORIES = ("short_visual", "speech", "cross_window", "no_match")
METRIC_VERSION = "frameseek-eval-0.1"


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Interval(Artifact):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Interval must have positive duration")
        return self


class EvalSource(Artifact):
    id: str = Field(min_length=1, max_length=128)
    kind: Literal["real", "synthetic"]
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Case(Artifact):
    id: str = Field(min_length=1, max_length=128)
    video_id: str
    category: Literal["short_visual", "speech", "cross_window", "no_match"]
    question: str = Field(min_length=1, max_length=4000)
    query_language: str = Field(min_length=1, max_length=30)
    audio_language: str | None = None
    screen_text_language: str | None = None
    annotation_state: Literal["draft", "reviewed"] = "draft"
    annotated_by: str | None = None
    events: list[Interval] = Field(default_factory=list, max_length=100)
    relevant_windows: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def annotations(self):
        if len(self.relevant_windows) != len(set(self.relevant_windows)):
            raise ValueError("Relevant windows must be unique")
        if self.category == "no_match" and (self.events or self.relevant_windows):
            raise ValueError("Negative cases cannot contain positive annotations")
        if self.annotation_state == "reviewed":
            if not self.annotated_by or not self.annotated_by.strip():
                raise ValueError("Reviewed annotations require a reviewer")
            if self.category != "no_match" and (not self.events or not self.relevant_windows):
                raise ValueError("Reviewed positive cases require events and relevant windows")
        return self


class Dataset(Artifact):
    schema_version: Literal["0.1"] = "0.1"
    id: str = Field(min_length=1, max_length=128)
    split: Literal["diagnostic", "development", "report"]
    sources: list[EvalSource] = Field(min_length=1, max_length=1000)
    cases: list[Case] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def identities(self):
        sources = {s.id for s in self.sources}
        if len(sources) != len(self.sources) or len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Source and case IDs must be unique")
        if any(c.video_id not in sources for c in self.cases):
            raise ValueError("Each case must belong to a declared source")
        if any(s.sha256 is None for s in self.sources) and any(
            c.annotation_state == "reviewed" for c in self.cases
        ):
            raise ValueError("Reviewed datasets require frozen source hashes")
        return self


class ClaimReview(Artifact):
    event_index: int = Field(ge=0)
    statement: str = Field(min_length=1, max_length=3000)
    supported: bool | None = None


class Result(Artifact):
    case_id: str
    outcome: Literal["completed", "timeout", "rejected", "failed"]
    response: Any = None
    raw_response: Any = None
    candidate_windows: list[str] | None = Field(default=None, max_length=1000)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    cost_micro: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=100)
    reviewed_response_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reviewed_by: str | None = Field(default=None, max_length=100)
    playback_verified: bool = False
    claim_inventory_complete: bool = False
    claims: list[ClaimReview] = Field(default_factory=list, max_length=1000)


class Run(Artifact):
    schema_version: Literal["0.1"] = "0.1"
    id: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["fixture", "real"]
    variant: Literal[
        "asr_keyword", "hybrid_one_zoom", "full_agent", "no_audio", "no_zoom", "no_hybrid"
    ]
    models: dict[str, str] = Field(default_factory=dict)
    configuration_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    currency_unit: str | None = Field(default=None, pattern=r"^[A-Z]{3}_MICRO$")
    results: list[Result] = Field(default_factory=list, max_length=10000)


def digest(value):
    """Canonical content hash: freeze actual annotations/results, not filenames."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def iou(a, b):
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return overlap / (max(a.end, b.end) - min(a.start, b.start))


def covers_events(truth, predicted):
    """Bipartite one-to-one coverage; a broad prediction cannot count as two events."""
    edges = [[j for j, p in enumerate(predicted) if iou(t, p) >= 0.5] for t in truth]
    assigned = {}

    def match(index, seen):
        for j in edges[index]:
            if j in seen:
                continue
            seen.add(j)
            if j not in assigned or match(assigned[j], seen):
                assigned[j] = index
                return True
        return False

    return bool(truth) and all(match(i, set()) for i in range(len(truth)))


def validated_answer(value, video_id):
    try:
        answer = Answer.model_validate(value)
        return answer if all(e.video_id == video_id for e in answer.events) else None
    except (ValidationError, TypeError, ValueError):
        return None


def score(dataset: Dataset, run: Run):
    if run.dataset_sha256 != digest(dataset):
        raise ValueError("Run does not match this frozen dataset")
    cases = {c.id: c for c in dataset.cases}
    result_ids = [r.case_id for r in run.results]
    if len(result_ids) != len(set(result_ids)) or any(i not in cases for i in result_ids):
        raise ValueError("Duplicate or undeclared result case IDs")
    results = {r.case_id: r for r in run.results}
    rows = []
    supported = total_claims = reviewed_claims = inventory_responses = 0
    eligible_responses = 0
    latencies, costs = [], []
    outcomes = Counter()
    for case in dataset.cases:
        result = results.get(case.id)
        outcome = result.outcome if result else "missing"
        outcomes[outcome] += 1
        answer = validated_answer(result.response, case.video_id) if result else None
        raw = validated_answer(result.raw_response, case.video_id) if result else None
        annotated = case.annotation_state == "reviewed"
        positive = case.category != "no_match"
        candidates = result.candidate_windows if result else None
        recall = None
        if annotated and positive:
            if outcome != "completed":
                recall = 0.0
            elif candidates is not None:
                recall = len(set(candidates[:5]) & set(case.relevant_windows)) / len(
                    case.relevant_windows
                )
        correct_negative = bool(outcome == "completed" and answer and answer.status == "no_result")
        event_success = (
            bool(outcome == "completed" and answer and covers_events(case.events, answer.events))
            if annotated and positive
            else None
        )
        reviewed = bool(
            result
            and answer
            and result.reviewed_by
            and result.reviewed_by.strip()
            and result.reviewed_response_sha256 == digest(result.response)
        )
        inventory = False
        if answer and answer.events:
            eligible_responses += 1
            inventory = bool(
                reviewed
                and result.claim_inventory_complete
                and result.claims
                and {c.event_index for c in result.claims} == set(range(len(answer.events)))
                and all(c.event_index < len(answer.events) for c in result.claims)
                and len({(c.event_index, c.statement) for c in result.claims}) == len(result.claims)
            )
            if inventory:
                inventory_responses += 1
                total_claims += len(result.claims)
                reviewed_claims += sum(c.supported is not None for c in result.claims)
                supported += sum(c.supported is True for c in result.claims)
        all_supported = bool(inventory and all(c.supported is True for c in result.claims))
        if result and outcome == "completed" and result.elapsed_seconds is not None:
            latencies.append(result.elapsed_seconds)
        if result and result.cost_micro is not None:
            costs.append(result.cost_micro)
        rows.append(
            {
                "case_id": case.id,
                "category": case.category,
                "outcome": outcome,
                "annotated": annotated,
                "schema_valid": answer is not None,
                "raw_schema_valid": raw is not None
                if result and result.raw_response is not None
                else None,
                "recall_at_5": recall,
                "iou_all_events_at_0_5": event_success,
                "negative_correct": correct_negative if not positive and annotated else None,
                "false_positive": bool(answer and answer.events)
                if not positive and annotated
                else None,
                "claims_complete_and_supported": all_supported,
                "playback_verified": bool(reviewed and result.playback_verified),
                "error_code": result.error_code if result else "missing_result",
                "query_language": case.query_language,
                "audio_language": case.audio_language,
                "screen_text_language": case.screen_text_language,
            }
        )
    positives = [r for r in rows if r["annotated"] and r["category"] != "no_match"]
    negatives = [r for r in rows if r["annotated"] and r["category"] == "no_match"]
    recalls = [r["recall_at_5"] for r in positives if r["recall_at_5"] is not None]
    raw_rows = [r for r in rows if r["raw_schema_valid"] is not None]
    ordered = sorted(latencies)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None
    counts = Counter(c.category for c in dataset.cases if c.annotation_state == "reviewed")
    gaps = []
    if dataset.split != "diagnostic":
        gaps.append("not_diagnostic_split")
    if any(counts[c] < 2 for c in CATEGORIES):
        gaps.append("need_two_reviewed_cases_per_category")
    if any(c.annotation_state != "reviewed" for c in dataset.cases):
        gaps.append("draft_annotations")
    if run.mode != "real" or any(s.kind != "real" for s in dataset.sources):
        gaps.append("fixture_or_synthetic_not_live_acceptance")
    if (
        not run.models
        or any(not v.strip() for v in run.models.values())
        or not run.configuration_sha256
    ):
        gaps.append("missing_model_revisions_or_configuration_hash")
    if any(not r["schema_valid"] or r["outcome"] != "completed" for r in rows):
        gaps.append("missing_failed_or_invalid_results")
    if any(r["recall_at_5"] != 1 for r in positives):
        gaps.append("positive_candidate_coverage_incomplete")
    if any(not r["iou_all_events_at_0_5"] for r in positives):
        gaps.append("positive_event_coverage_incomplete")
    if any(not r["negative_correct"] for r in negatives):
        gaps.append("negative_cases_not_correct_no_result")
    if any(not r["claims_complete_and_supported"] for r in positives):
        gaps.append("human_claim_support_review_incomplete_or_failed")
    if any(not r["playback_verified"] for r in rows):
        gaps.append("human_playback_review_incomplete")
    metrics = {
        "requests": len(rows),
        "outcomes": dict(outcomes),
        "completion_rate": outcomes["completed"] / len(rows),
        "schema_final_rate": sum(r["schema_valid"] for r in rows) / len(rows),
        "schema_raw_rate": sum(r["raw_schema_valid"] for r in raw_rows) / len(rows)
        if raw_rows
        else None,
        "schema_raw_observed": len(raw_rows),
        "positive_questions": len(positives),
        "negative_questions": len(negatives),
        "recall_at_5_macro": sum(recalls) / len(recalls)
        if recalls and len(recalls) == len(positives)
        else None,
        "recall_observed_questions": len(recalls),
        "iou_all_events_at_0_5_rate": sum(bool(r["iou_all_events_at_0_5"]) for r in positives)
        / len(positives)
        if positives
        else None,
        "negative_correct_rate": sum(r["negative_correct"] for r in negatives) / len(negatives)
        if negatives
        else None,
        "negative_false_positive_rate": sum(r["false_positive"] for r in negatives) / len(negatives)
        if negatives
        else None,
        "evidence_accuracy": supported / total_claims
        if total_claims
        and reviewed_claims == total_claims
        and inventory_responses == eligible_responses
        else None,
        "claims_total_in_complete_inventories": total_claims,
        "claims_reviewed": reviewed_claims,
        "claim_inventories_complete": inventory_responses,
        "responses_requiring_claim_inventory": eligible_responses,
        "latency_p95_completed_seconds": p95,
        "latency_observed_completed": len(latencies),
        "cost_observed_requests": len(costs),
        "cost_total_micro": sum(costs) if len(costs) == len(rows) and run.currency_unit else None,
        "cost_observed_micro": sum(costs) if costs and run.currency_unit else None,
        "currency_unit": run.currency_unit,
    }
    return {
        "metric_version": METRIC_VERSION,
        "dataset_id": dataset.id,
        "dataset_sha256": digest(dataset),
        "run_id": run.id,
        "mode": run.mode,
        "variant": run.variant,
        "models": run.models,
        "configuration_sha256": run.configuration_sha256,
        "metrics": metrics,
        "diagnostic_gate": {
            "status": "GO" if not gaps else "NOT_ACCEPTED",
            "gaps": gaps,
            "scope": "diagnostic only; not full MVP or performance acceptance",
        },
        "cases": rows,
    }


def read_artifact(path, model):
    if path.stat().st_size > 16 * 1024**2:
        raise ValueError("Evaluation artifact exceeds 16 MiB")
    return model.model_validate_json(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "score"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        dataset = read_artifact(args.dataset, Dataset)
        if args.action == "validate":
            result = {
                "dataset_id": dataset.id,
                "dataset_sha256": digest(dataset),
                "cases": len(dataset.cases),
                "reviewed_cases": sum(c.annotation_state == "reviewed" for c in dataset.cases),
                "categories": dict(Counter(c.category for c in dataset.cases)),
                "note": "Structure validation is not live quality acceptance.",
            }
        else:
            if args.run is None:
                parser.error("score requires --run")
            result = score(dataset, read_artifact(args.run, Run))
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            # Reports may contain private annotations; never overwrite an existing artifact.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
        print(text, end="")
    except (OSError, ValueError, ValidationError):
        parser.exit(
            2, "Invalid evaluation artifact, dataset/run mismatch, or output unavailable.\n"
        )


if __name__ == "__main__":
    main()
