"""Score reviewed sampled-frame cases without calling or trusting a model."""

import math
import re

REQUIRED_CATEGORIES = {"person", "object", "appearance", "distractor", "text", "followup", "seek"}


def _valid_seek(check, predicted, frame_times):
    if not isinstance(check, dict) or check.get("reviewed") is not True:
        return False
    frame_id = check.get("frame_id")
    target, actual = check.get("target_time"), check.get("player_time")
    reference = check.get("evidence_reference")
    return (
        isinstance(frame_id, str)
        and frame_id in predicted
        and all(type(value) in (int, float) and math.isfinite(value) for value in (target, actual))
        and abs(target - frame_times[frame_id]) < 0.001
        and abs(actual - target) <= 0.25
        and isinstance(reference, str)
        and bool(reference.strip())
    )


def catalog_from_index(index, *, index_version, source_kind):
    """Build a review catalog from an already validated, owned index version.

    The caller must load and verify the published version and its source first;
    constructing a catalog does not attest to human labels or model accuracy.
    """
    from frameseek.occurrences import ObservationIndex

    if not isinstance(index, ObservationIndex):
        raise ValueError("invalid_evaluation_index")
    if source_kind not in {"real", "synthetic"} or not isinstance(index_version, str):
        raise ValueError("invalid_evaluation_identity")
    if not re.fullmatch(r"[a-f0-9]{64}", index_version):
        raise ValueError("invalid_evaluation_identity")
    return {
        index.video: {
            "source_kind": source_kind,
            "source_fingerprint": index.source_fingerprint,
            "index_version": index_version,
            "duration": index.duration,
            "frames": [{"id": frame.frame_id, "t": frame.t} for frame in index.frames],
        }
    }


def prepare_review_case(
    index, *, index_version, source_kind, case_id, question, start, end, categories
):
    """Create an unlabelled case from a validated index; never infer human review.

    Frame URLs remain owner-protected and are references, not copied media.
    Prediction and every human label must be filled before scoring.
    """
    catalog_from_index(index, index_version=index_version, source_kind=source_kind)
    if (
        not isinstance(case_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", case_id)
        or not isinstance(question, str)
        or not question.strip()
        or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in (start, end)
        )
        or not 0 <= start < end <= index.duration
        or not isinstance(categories, list)
        or any(not isinstance(item, str) or item not in REQUIRED_CATEGORIES for item in categories)
        or len(set(categories)) != len(categories)
    ):
        raise ValueError("invalid_review_case")
    frames = [frame for frame in index.frames if start <= frame.t < end]
    if not frames:
        raise ValueError("review_scope_has_no_sampled_frames")
    return {
        "id": case_id,
        "source_kind": source_kind,
        "source_id": index.video,
        "source_fingerprint": index.source_fingerprint,
        "index_version": index_version,
        "question": question,
        "scope_start": start,
        "scope_end": end,
        "categories": categories,
        "sampled_frames": [frame.frame_id for frame in frames],
        "frame_references": [
            {"frame_id": frame.frame_id, "t": frame.t, "uri": frame.uri} for frame in frames
        ],
        "frame_labels": [{"frame_id": frame.frame_id, "visible": None} for frame in frames],
        "expected": None,
        "predicted": None,
        "visible_duration_seconds": None,
        "reviewed": False,
        "reviewer": "",
    }


def sampled_index_predictions(index, *, index_version, source_kind, question, start, end):
    """Collect every sampled-frame hit from the index, without calling a model.

    This evaluates structured retrieval, not the complete browser journey or
    recognition accuracy. Full-frame catalogs can merge hits into intervals and
    are intentionally excluded from this sampled-frame evaluator.
    """
    from frameseek.agent import AgentError, explicit_time_window
    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import remove_time_range, resolve_conditions
    from frameseek.visual_search import requires_aggregation

    catalog_from_index(index, index_version=index_version, source_kind=source_kind)
    if index.catalog_complete:
        raise ValueError("sampled_review_requires_sampled_index")
    if (
        not isinstance(question, str)
        or not question.strip()
        or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in (start, end)
        )
        or not 0 <= start < end <= index.duration
    ):
        raise ValueError("invalid_review_case")
    if requires_aggregation(question):
        raise ValueError("sampled_review_does_not_support_event_counts")
    try:
        specified = explicit_time_window(question, index.duration, max_seconds=3600)
    except AgentError as error:
        raise ValueError("invalid_review_question_scope") from error
    scope = {"start": start, "end": end}
    if specified and specified != scope:
        raise ValueError("review_scope_mismatch")
    predicates, problems = resolve_conditions(
        index, remove_time_range(question) if specified else question
    )
    if problems:
        return {"kind": "clarification_required", "problems": problems, "model_requests_sent": 0}
    conditions = [item.model_dump() for item in predicates]
    offset, snapshot, frames = 0, None, []
    while True:
        page = search_occurrences(
            index, conditions, offset=offset, limit=100, snapshot=snapshot, time_window=scope
        )
        snapshot = page["snapshot"]
        frames.extend(
            evidence["frame_id"] for hit in page["occurrences"] for evidence in hit["evidence"]
        )
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    return {
        "kind": "sampled_index_predictions",
        "source_id": index.video,
        "source_kind": source_kind,
        "source_fingerprint": index.source_fingerprint,
        "index_version": index_version,
        "index_snapshot": index.snapshot(),
        "question": question,
        "scope_start": start,
        "scope_end": end,
        "predicted": frames,
        "model_requests_sent": 0,
    }


def score_cases(cases, *, source_catalog=None):
    """Return separate real/synthetic metrics; incomplete labels cannot pass.

    Each case is a human-reviewed query with its sampled frame universe, positive
    labels and returned frame IDs. The catalog must come from validated source
    versions and cover every sampled frame within each case's scope. Duration
    eligibility does not claim every original video frame was examined.
    """
    seen, seen_scopes = set(), set()
    groups = {
        "real": {"cases": 0, "tp": 0, "fp": 0, "fn": 0},
        "synthetic": {"cases": 0, "tp": 0, "fp": 0, "fn": 0},
    }
    categories, issues = set(), []
    if source_catalog is None:
        source_catalog = {}
        issues.append("source_catalog_missing")
    elif not isinstance(source_catalog, dict):
        raise ValueError("invalid_evaluation_catalog")
    for case in cases:
        key = case.get("id")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("duplicate_or_missing_case_id")
        seen.add(key)
        kind = case.get("source_kind")
        if kind not in groups:
            raise ValueError("invalid_evaluation_source_kind")
        source_id = case.get("source_id")
        question = case.get("question")
        catalog = source_catalog.get(source_id) if isinstance(source_id, str) else None
        if not isinstance(question, str) or not question.strip() or not isinstance(catalog, dict):
            issues.append(f"{key}:source_or_question_missing")
            continue
        start, end = case.get("scope_start"), case.get("scope_end")
        duration_limit = catalog.get("duration")
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end, duration_limit)
            )
            or not 0 <= start < end <= duration_limit
            or catalog.get("source_kind") != kind
            or not isinstance(catalog.get("source_fingerprint"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", catalog["source_fingerprint"])
            or not isinstance(catalog.get("index_version"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", catalog["index_version"])
            or case.get("source_fingerprint") != catalog["source_fingerprint"]
            or case.get("index_version") != catalog["index_version"]
        ):
            issues.append(f"{key}:source_or_scope_invalid")
            continue
        identity = (source_id, question.strip().casefold(), start, end)
        if identity in seen_scopes:
            issues.append(f"{key}:duplicate_case")
            continue
        seen_scopes.add(identity)
        if (
            case.get("reviewed") is not True
            or not isinstance(case.get("reviewer"), str)
            or not case["reviewer"].strip()
        ):
            issues.append(f"{key}:not_human_reviewed")
            continue
        duration = case.get("visible_duration_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 1:
            issues.append(f"{key}:duration_ineligible")
            continue
        sample, expected, predicted = (
            case.get(name) for name in ("sampled_frames", "expected", "predicted")
        )
        if any(
            not isinstance(value, list)
            or any(not isinstance(x, str) or not x for x in value)
            or len(set(value)) != len(value)
            for value in (sample, expected, predicted)
        ):
            raise ValueError("invalid_evaluation_frame_set")
        sample, expected, predicted = set(sample), set(expected), set(predicted)
        frames = catalog.get("frames")
        if not isinstance(frames, list) or any(
            not isinstance(frame, dict)
            or not isinstance(frame.get("id"), str)
            or not frame["id"]
            or type(frame.get("t")) not in (int, float)
            or not math.isfinite(frame["t"])
            or not 0 <= frame["t"] < duration_limit
            for frame in frames
        ):
            raise ValueError("invalid_evaluation_catalog")
        ids = [frame["id"] for frame in frames]
        times = [frame["t"] for frame in frames]
        if len(set(ids)) != len(ids) or any(a >= b for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("invalid_evaluation_catalog")
        scoped = {frame["id"] for frame in frames if start <= frame["t"] < end}
        if not sample or sample != scoped or not expected <= sample or not predicted <= sample:
            issues.append(f"{key}:incomplete_frame_labels")
            continue
        labels = case.get("frame_labels")
        if (
            not isinstance(labels, list)
            or any(
                not isinstance(label, dict)
                or set(label) != {"frame_id", "visible"}
                or not isinstance(label["frame_id"], str)
                or type(label["visible"]) is not bool
                for label in labels
            )
            or len(labels) != len(sample)
            or len({label["frame_id"] for label in labels}) != len(labels)
            or {label["frame_id"] for label in labels} != sample
            or {label["frame_id"] for label in labels if label["visible"]} != expected
        ):
            issues.append(f"{key}:frame_reviews_incomplete")
            continue
        group = groups[kind]
        group["cases"] += 1
        group["tp"] += len(predicted & expected)
        group["fp"] += len(predicted - expected)
        group["fn"] += len(expected - predicted)
        case_categories = case.get("categories", [])
        if not isinstance(case_categories, list) or any(
            not isinstance(c, str) for c in case_categories
        ):
            raise ValueError("invalid_evaluation_categories")
        checked_categories = set(case_categories)
        if "followup" in checked_categories:
            check = case.get("followup_check")
            if (
                not isinstance(check, dict)
                or check.get("reviewed") is not True
                or not all(
                    isinstance(check.get(field), str) and check[field].strip()
                    for field in ("parent_request_id", "resolved_question", "evidence_reference")
                )
                or check.get("source_fingerprint") != catalog["source_fingerprint"]
                or check.get("index_version") != catalog["index_version"]
            ):
                checked_categories.remove("followup")
                issues.append(f"{key}:followup_unverified")
        if "seek" in checked_categories:
            checks = case.get("seek_checks")
            frame_times = {frame["id"]: frame["t"] for frame in frames}

            if (
                not isinstance(checks, list)
                or not checks
                or not all(_valid_seek(check, predicted, frame_times) for check in checks)
            ):
                checked_categories.remove("seek")
                issues.append(f"{key}:seek_unverified")
        categories.update(checked_categories)
    for group in groups.values():
        group["precision"] = (
            group["tp"] / (group["tp"] + group["fp"]) if group["tp"] + group["fp"] else None
        )
        group["recall"] = (
            group["tp"] / (group["tp"] + group["fn"]) if group["tp"] + group["fn"] else None
        )
    tp, fp, fn = (sum(group[name] for group in groups.values()) for name in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    count = sum(group["cases"] for group in groups.values())
    missing = sorted(REQUIRED_CATEGORIES - categories)
    accepted = (
        count >= 20
        and all(group["cases"] for group in groups.values())
        and all(
            group["precision"] is not None
            and group["precision"] >= 0.90
            and group["recall"] is not None
            and group["recall"] >= 0.85
            for group in groups.values()
        )
        and not issues
        and not missing
        and precision is not None
        and precision >= 0.90
        and recall is not None
        and recall >= 0.85
    )
    return {
        "accepted": accepted,
        "cases": count,
        "precision": precision,
        "recall": recall,
        "groups": groups,
        "missing_categories": missing,
        "issues": issues,
        "coverage": "reviewed_sampled_frames_only",
    }
