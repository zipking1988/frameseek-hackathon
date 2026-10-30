"""Bounded, read-only supplement planning from a published observation index.

Candidates are leads, never facts or model calls. No candidates means a coverage
gap rather than permission to scan the whole video.
"""

import math
import re

from frameseek.auto_routing import needs_audio
from frameseek.occurrences import Condition, fingerprint, subject_verdict
from frameseek.semantic_search import (
    canonical,
    caption_terms,
    interpret,
    remove_time_range,
    resolve_conditions,
)
from frameseek.visual_search import requires_aggregation


def plan_supplement(index, question, version, *, max_windows=2):
    from frameseek.agent import AgentError, explicit_time_window

    if type(max_windows) is not int or not 1 <= max_windows <= 2:
        raise ValueError("knowledge_supplement_window_limit")
    if not re.fullmatch(r"[a-f0-9]{64}", version or ""):
        raise ValueError("invalid_knowledge_version")
    common = {
        "video_id": index.video,
        "generation": index.generation,
        "knowledge_version": version,
        "model_requests_sent": 0,
    }
    if needs_audio(question) or requires_aggregation(question):
        return {**common, "kind": "capability_missing", "windows": []}
    try:
        scope = explicit_time_window(question, index.duration, max_seconds=3600)
    except AgentError:
        return {
            **common,
            "kind": "clarification_required",
            "windows": [],
            "problems": ["The requested video time range is invalid."],
        }
    query = remove_time_range(question) if scope else question
    terms, problems = interpret(query)
    if problems:
        return {**common, "kind": "clarification_required", "windows": [], "problems": problems}
    options = {}
    for observation in index.observations:
        for subject in observation.subjects:
            for field, values in subject.facts.items():
                for value in values:
                    options.setdefault(canonical(value), set()).add((field, value))
    grounded, missing = [], []
    for term in terms:
        values = options.get(term, set())
        fields = {field for field, _ in values}
        if len(fields) == 1:
            # Values such as person/player normalize to the same concept. A
            # single field remains grounded even when the source uses both.
            grounded.append(Condition(field=next(iter(fields)), value=term))
        else:
            missing.append(term)
    observations = {row.frame_id: row for row in index.observations}
    exact_conditions, exact_problems = resolve_conditions(index, query)
    windows = {}
    for frame in index.frames:
        if scope and not scope["start"] <= frame.t < scope["end"]:
            continue
        observation = observations.get(frame.frame_id)
        if not observation:
            continue
        candidates = observation.subjects
        if not exact_problems and exact_conditions:
            verdicts = [subject_verdict(subject, exact_conditions) for subject in candidates]
            if observation.inventory_complete and not any(
                verdict in {"yes", "unknown"} for verdict in verdicts
            ):
                # The existing complete inventory already rejects this target;
                # a partial match on e.g. "person" is not a missing attribute.
                continue
            if "yes" in verdicts:
                # A confirmed person does not resolve another person's missing
                # attributes in the same frame. Preserve the existing hit while
                # allowing bounded re-examination of the uncertain subject.
                candidates = [
                    subject
                    for subject, verdict in zip(candidates, verdicts, strict=True)
                    if verdict == "unknown"
                ]
                if not candidates:
                    continue
        # Score one subject at a time. Similarity is used for candidates only.
        best = max(
            (
                sum(subject_verdict(subject, [condition]) == "yes" for condition in grounded)
                for subject in candidates
            ),
            default=0,
        )
        if best == 0:
            continue
        # A caption can rank a grounded lead, never establish a match. This
        # prioritizes a window describing the missing attributes without
        # turning a free-text description into subject-bound evidence.
        best += len(set(terms) & caption_terms(frame.caption))
        ordinal = math.floor(frame.t / 8)
        entry = windows.setdefault(ordinal, {"score": 0, "frame_ids": []})
        entry["score"] = max(entry["score"], best)
        entry["frame_ids"].append(frame.frame_id)
    selected = sorted(windows, key=lambda ordinal: (-windows[ordinal]["score"], ordinal))[
        :max_windows
    ]
    # A user's short explicit time range is itself a location, even when the
    # base extractor omitted the requested text or object entirely. Never turn
    # this into a whole-video scan, and do not recheck already confirmed hits.
    if scope:
        first = math.floor(scope["start"] / 8)
        last = math.ceil(scope["end"] / 8)
        if last - first <= max_windows:
            for frame in index.frames:
                if scope["start"] <= frame.t < scope["end"]:
                    observation = observations.get(frame.frame_id)
                    if not exact_problems and exact_conditions and observation:
                        verdicts = [
                            subject_verdict(subject, exact_conditions)
                            for subject in observation.subjects
                        ]
                        # Preserve existing positive evidence without paying to
                        # re-examine it. A complete, unambiguous negative also
                        # needs no supplement. Missing/incomplete frames do.
                        if (
                            "yes" in verdicts
                            and (len(exact_conditions) == 1 or "unknown" not in verdicts)
                        ) or (observation.inventory_complete and "unknown" not in verdicts):
                            continue
                    ordinal = math.floor(frame.t / 8)
                    entry = windows.setdefault(ordinal, {"score": 0, "frame_ids": []})
                    if frame.frame_id not in entry["frame_ids"]:
                        entry["frame_ids"].append(frame.frame_id)
            selected = sorted(windows)
            positions = {frame.frame_id: frame.ordinal for frame in index.frames}
            for ordinal in selected:
                windows[ordinal]["frame_ids"].sort(key=positions.__getitem__)
    if not selected:
        return {**common, "kind": "coverage_gap", "windows": [], "missing_terms": missing}
    selected.sort()
    plan = [
        {
            "ordinal": ordinal,
            "start": max(float(ordinal * 8), float(scope["start"]) if scope else 0.0),
            "end": min(
                index.duration,
                float((ordinal + 1) * 8),
                float(scope["end"]) if scope else index.duration,
            ),
            "frame_ids": windows[ordinal]["frame_ids"][:8],
        }
        for ordinal in selected
    ]
    return {
        **common,
        "kind": "supplement_candidates",
        "windows": plan,
        "missing_terms": missing,
        "candidates_are_evidence": False,
        "plan_id": fingerprint({"version": version, "question": question, "windows": plan}),
    }
