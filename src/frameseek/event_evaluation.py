"""Offline event-localization benchmark. No inference, network or workspace writes.

Reviewed clip labels are evaluation artifacts, never observations imported into
production. Recall is scoped to reviewed intervals, not the whole source.
"""

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from frameseek.evaluation import Artifact, Interval


class EventSource(Artifact):
    id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration: float = Field(gt=0)
    split: Literal["development", "held_out"]
    group: str = Field(min_length=1)
    reviewed: list[Interval]

    @model_validator(mode="after")
    def coverage(self):
        previous = 0.0
        for region in self.reviewed:
            if region.start < previous or region.end > self.duration:
                raise ValueError("reviewed intervals must be ordered, disjoint and in source")
            previous = region.end
        return self


class EventLabel(Interval):
    id: str = Field(min_length=1)
    source: str
    label: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    verdict: Literal["confirmed", "uncertain"]
    basis: str = Field(min_length=1)
    evidence_times: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_bounds(self):
        if any(not self.start <= t <= self.end for t in self.evidence_times):
            raise ValueError("evidence time outside event")
        return self


class EventBenchmark(Artifact):
    version: Literal["event-eval-1"] = "event-eval-1"
    sources: list[EventSource] = Field(min_length=1)
    labels: list[EventLabel]

    @model_validator(mode="after")
    def ownership(self):
        sources = {s.id: s for s in self.sources}
        if len(sources) != len(self.sources) or len({s.sha256 for s in self.sources}) != len(
            self.sources
        ):
            raise ValueError("duplicate source or source leaked across splits")
        group_splits = {}
        for source in self.sources:
            if source.group in group_splits and group_splits[source.group] != source.split:
                raise ValueError("source group leaked across splits")
            group_splits[source.group] = source.split
        if len({e.id for e in self.labels}) != len(self.labels):
            raise ValueError("duplicate event id")
        for event in self.labels:
            source = sources.get(event.source)
            if source is None or not any(
                r.start <= event.start < event.end <= r.end for r in source.reviewed
            ):
                raise ValueError("event outside reviewed source coverage")
        groups = {}
        for event in self.labels:
            key = (event.source, event.canonical)
            signature = (event.label, event.verdict)
            if key in groups and groups[key] != signature:
                raise ValueError("conflicting replay group")
            groups[key] = signature
        return self


class EventPrediction(Interval):
    id: str = Field(min_length=1)
    source: str
    label: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    evidence_times: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_bounds(self):
        if any(not self.start <= t <= self.end for t in self.evidence_times):
            raise ValueError("evidence time outside prediction")
        return self


class EventRun(Artifact):
    version: Literal["event-run-1"] = "event-run-1"
    extractor: str = Field(min_length=1)
    source_hashes: dict[str, str]
    predictions: list[EventPrediction]


def temporal_iou(a, b):
    intersection = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return intersection / (max(a.end, b.end) - min(a.start, b.start))


def score_events(benchmark, run, threshold=0.5):
    """Maximum one-to-one matching; replay aliases cannot hide unrelated events."""
    if not 0 < threshold <= 1:
        raise ValueError("invalid IoU threshold")
    sources = {s.id: s for s in benchmark.sources}
    if run.source_hashes != {s.id: s.sha256 for s in benchmark.sources}:
        raise ValueError("source fingerprint mismatch")
    if len({p.id for p in run.predictions}) != len(run.predictions):
        raise ValueError("duplicate prediction id")
    confirmed = [e for e in benchmark.labels if e.verdict == "confirmed"]
    uncertain = [e for e in benchmark.labels if e.verdict == "uncertain"]
    ignored, outside, evaluated = [], [], []
    for prediction in run.predictions:
        source = sources.get(prediction.source)
        if source is None or prediction.end > source.duration:
            raise ValueError("prediction outside source")
        if not any(r.start <= prediction.start < prediction.end <= r.end for r in source.reviewed):
            outside.append(prediction.id)
        elif not any(
            prediction.source == e.source and temporal_iou(prediction, e) >= threshold
            for e in confirmed
        ) and any(
            prediction.source == e.source and temporal_iou(prediction, e) > 0 for e in uncertain
        ):
            ignored.append(prediction.id)
        else:
            evaluated.append(prediction)
    edges = {
        p.id: [
            e.id
            for e in confirmed
            if p.source == e.source and p.label == e.label and temporal_iou(p, e) >= threshold
        ]
        for p in evaluated
    }
    matches = {}

    def augment(prediction, visited):
        for event in edges[prediction]:
            if event in visited:
                continue
            visited.add(event)
            if event not in matches or augment(matches[event], visited):
                matches[event] = prediction
                return True
        return False

    for prediction in evaluated:
        augment(prediction.id, set())
    true_positive = len(matches)
    false_positive = len(evaluated) - true_positive
    false_negative = len(confirmed) - true_positive
    actual_groups = {(e.source, e.label, e.canonical) for e in confirmed}
    predicted_groups = {(p.source, p.label, p.canonical) for p in evaluated}
    count_keys = sorted({(source, label) for source, label, _ in actual_groups | predicted_groups})
    counts = [
        {
            "source": source,
            "label": label,
            "split": sources[source].split,
            "expected": sum((s, label_) == (source, label) for s, label_, _ in actual_groups),
            "predicted": sum((s, label_) == (source, label) for s, label_, _ in predicted_groups),
        }
        for source, label in count_keys
    ]
    # A declared replay group spanning two distinct ground-truth events is wrong,
    # even if its headline count happens to agree through another missed event.
    group_targets = {}
    by_prediction = {p.id: p for p in evaluated}
    by_event = {e.id: e for e in confirmed}
    for event_id, prediction_id in matches.items():
        p, e = by_prediction[prediction_id], by_event[event_id]
        group_targets.setdefault((p.source, p.label, p.canonical), set()).add(e.canonical)
    return {
        "version": "event-eval-1",
        "extractor": run.extractor,
        "scope": "reviewed_intervals_only",
        "iou_threshold": threshold,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": true_positive / len(evaluated) if evaluated else None,
        "recall": true_positive / len(confirmed) if confirmed else None,
        "missed_event_ids": sorted(set(by_event) - set(matches)),
        "false_positive_ids": sorted(set(by_prediction) - set(matches.values())),
        "uncertain_prediction_ids": ignored,
        "unreviewed_prediction_ids": outside,
        "reviewed_unique_events": len(actual_groups),
        "predicted_unique_events": len(predicted_groups),
        "counts": counts,
        "count_absolute_error": sum(abs(c["expected"] - c["predicted"]) for c in counts),
        "incorrect_replay_merges": sum(len(targets) > 1 for targets in group_targets.values()),
        "full_source_reviewed": all(
            len(s.reviewed) == 1 and s.reviewed[0].start == 0 and s.reviewed[0].end == s.duration
            for s in benchmark.sources
        ),
        "held_out_sources": sum(s.split == "held_out" for s in benchmark.sources),
        "training_decision": "not_established_by_event_metrics_alone",
        "model_requests_sent": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("run", type=Path)
    parser.add_argument("--iou", type=float, default=0.5)
    args = parser.parse_args()
    benchmark = EventBenchmark.model_validate_json(args.benchmark.read_text())
    run = EventRun.model_validate_json(args.run.read_text())
    print(json.dumps(score_events(benchmark, run, args.iou), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
