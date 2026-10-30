"""Owned callers bridge private reusable indexes to search and durable chat."""

import hashlib
import json
import os

from frameseek.media import MAX_UPLOAD
from frameseek.occurrences import fingerprint, load_index, search_occurrences
from frameseek.schema import Answer, hms
from frameseek.semantic_search import (
    available_conditions,
    caption_matches,
    interpret,
    remove_time_range,
    resolve_conditions,
)
from frameseek.visual_search import compile_index, requires_aggregation


def source_digest(directory):
    digest = hashlib.sha256()
    fd = os.open(directory / "original", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_UPLOAD:
            raise ValueError("source_limit")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def search_source(
    root,
    source,
    question,
    offset=0,
    limit=40,
    snapshot=None,
    conditions=None,
    knowledge=None,
    candidate_offset=0,
    candidate_time=None,
    include_candidates=False,
):
    aggregate = not conditions and requires_aggregation(question)
    video, generation = source["id"], source["generation"]
    directory = root / video
    metadata = json.loads(source["metadata"])
    from frameseek.agent import AgentError, explicit_time_window

    try:
        time_window = explicit_time_window(question, metadata["duration"], max_seconds=3600)
    except AgentError:
        return {
            "kind": "clarification_required",
            "message": "The requested video time range is invalid.",
            "available_conditions": [],
            "model_requests_sent": 0,
        }
    query_for_terms = remove_time_range(question) if time_window else question
    artifact = directory / f"observations-g{generation}.json"
    if knowledge is not None or artifact.exists() or artifact.is_symlink():
        index = knowledge or load_index(
            artifact,
            video=video,
            generation=generation,
            source_fingerprint=source_digest(directory),
        )
        if index.duration != metadata["duration"]:
            raise ValueError("stale_index_duration")
        if aggregate:
            if time_window:
                return {
                    "kind": "clarification_required",
                    "message": "Event statistics do not support a time "
                    "range; unobserved events are not inferred.",
                    "available_conditions": [],
                    "model_requests_sent": 0,
                }
            from frameseek.event_search import search_events

            result = search_events(index, question, offset, limit, snapshot)
            if result["kind"] == "clarification_required":
                return result
            result["provenance"] = index.extractor_revision
        else:
            predicates, problems = resolve_conditions(index, query_for_terms, conditions)
            if problems:
                return {
                    "kind": "clarification_required",
                    "message": " ".join(problems),
                    "available_conditions": available_conditions(index),
                    "model_requests_sent": 0,
                }
            result = search_occurrences(
                index,
                predicates,
                offset,
                limit,
                snapshot,
                time_window=time_window,
                candidate_offset=candidate_offset,
                candidate_time=candidate_time,
                include_candidates=include_candidates,
            )
            result.update(
                conditions=[c.model_dump() for c in predicates],
                available_conditions=available_conditions(index),
                structured_conditions_evaluated=True,
                verified_matches=False,
                explicit_conditions=bool(conditions),
                provenance=index.extractor_revision,
            )
        frames = [e for hit in result["occurrences"] for e in hit["evidence"]]
        frames.extend(result.get("uncertain_candidates", []))
    else:
        if aggregate:
            return {
                "kind": "clarification_required",
                "message": (
                    "This video has no individual event observations. Sampled descriptions cannot "
                    "establish scores, totals or rankings. Your "
                    "question was kept; no model was called."
                ),
                "available_conditions": [],
                "model_requests_sent": 0,
            }
        if conditions:
            return {
                "kind": "clarification_required",
                "message": "Only frame descriptions are available; there "
                "is no same-object observation index.",
                "available_conditions": [],
                "model_requests_sent": 0,
            }
        terms, problems = interpret(query_for_terms)
        if problems or not terms:
            return {
                "kind": "clarification_required",
                "message": " ".join(problems)
                or "Specify the object and visible conditions to find.",
                "available_conditions": [],
                "model_requests_sent": 0,
            }
        if not source["manifest"]:
            raise ValueError("video_not_ready")
        index = compile_index(
            video, generation, metadata["duration"], json.loads(source["manifest"])
        )
        identity = fingerprint(
            {"index": index["fingerprint"], "question": question, "terms": terms}
        )
        if (offset and snapshot is None) or snapshot is not None and snapshot != identity:
            raise ValueError("index_snapshot_conflict")
        matches = [
            frame
            for frame in index["frames"]
            if (not time_window or time_window["start"] <= frame["t"] < time_window["end"])
            and caption_matches(frame["caption"], terms)
        ]
        end = offset + limit
        result = {
            "kind": "index_candidates",
            "frames": matches[offset:end],
            "total_candidates": len(matches),
            "indexed_frames": len(index["frames"]),
            "next_offset": end if end < len(matches) else None,
            "snapshot": identity,
            "verified_matches": False,
            "model_requests_sent": 0,
            "terms": terms,
            "available_conditions": [],
            "provenance": "legacy_saved_captions",
            "coverage": {
                "catalog_complete": False,
                "reviewed_frames": len(index["frames"]),
                "exhaustive_under_index": False,
                "all_source_frames_reviewed": False,
                "recognition_accuracy_verified": False,
            },
        }
        frames = result["frames"]
    for frame in frames:
        file = frame["uri"].rsplit("/", 1)[-1]
        path = directory / "frames" / file
        if (
            not path.is_file()
            or path.is_symlink()
            or path.resolve().parent != (directory / "frames").resolve()
        ):
            raise ValueError("indexed_evidence_missing")
    result.update(
        video_id=video, generation=generation, question=question, duration=metadata["duration"]
    )
    return result


def chat_answer(result):
    """Only supplied structured observations become evidence events, never captions."""
    events = []
    coverage = result["coverage"]
    if result["kind"] == "indexed_occurrences":
        for hit in result["occurrences"][:100]:
            # A fully catalogued frame can begin just before the requested
            # window but remain visible inside it. Keep its real presentation
            # timestamp within the answer event rather than inventing a frame.
            start = min(hit["start"], *(e["t"] for e in hit["evidence"]))
            end = hit["end"] if hit["end"] is not None else min(result["duration"], start + 0.001)
            if end <= start:
                continue
            events.append(
                {
                    "video_id": result["video_id"],
                    "start": start,
                    "end": end,
                    "start_hms": hms(start),
                    "end_hms": hms(end),
                    "action": result.get("aggregation", {}).get(
                        "action", "Matching saved observation"
                    ),
                    "summary": (
                        "Evidence from a saved event observation; "
                        "replays of the same confirmed event count "
                        "once."
                        if result.get("aggregation")
                        else (
                            "A subject in the saved frame matches all "
                            "search conditions; identity across shots is "
                            "not inferred. "
                            + (
                                "The first frame appears before the selected "
                                "range and continues into it."
                                if start < hit["start"]
                                else ""
                            )
                        )
                    ),
                    "support_level": "low",
                    "evidence": [
                        {
                            "frame_id": e["frame_id"],
                            "t": e["t"],
                            "t_hms": hms(e["t"]),
                            "uri": e["uri"],
                            "caption": (
                                "Source frame from a saved event observation; no new model review."
                                if result.get("aggregation")
                                else "The index matches the conditions; "
                                "recognition is unverified. Review the source "
                                "frame."
                            ),
                            "asr": None,
                        }
                        for e in hit["evidence"]
                    ],
                }
            )
        total = result["total_occurrences"]
        text = (
            (
                f"Found {total} matching sampled frame times; "
                "this is not a count of people or events."
                if coverage.get("catalog_complete") is False and not result.get("aggregation")
                else f"Found {total} intervals or times in the saved observation index."
            )
            if total
            else "No saved observations match all conditions."
        )
        if total and "有没有" in result.get("question", "") and not result.get("aggregation"):
            text = "The index contains a match. " + text
        if events and not result.get("aggregation"):
            # Quote actual frame timestamps, not the edges of an inferred span.
            times = list(dict.fromkeys(e["t_hms"] for event in events for e in event["evidence"]))
            if times:
                label = (
                    "Some cited frame times"
                    if len(times) > 3 or result.get("next_offset")
                    else "Cited frame times"
                )
                text += f" {label}: {', '.join(times[:3])}."
        if not coverage["exhaustive_under_index"]:
            text += " Unchecked or uncertain ranges prevent a claim of all appearances or absence."
        else:
            text += (
                " All indexed source frames were searched; "
                "accuracy depends on the original "
                "observations. No new visual review was "
                "performed."
            )
        if events and coverage.get("recognition_accuracy_verified") is not True:
            text += " Recognition is unverified; review the cited frames."
        if any(
            condition.get("value") in {"male", "female"}
            for condition in result.get("conditions", [])
        ):
            text += (
                " Gender-related labels come from saved "
                "observations; identity and gender identity "
                "are unverified."
            )
    else:
        total = result["total_candidates"]
        text = (
            f"Found {total} candidate frames whose saved descriptions contain the search concepts. "
            "The video lacks a complete object "
            "observation index, so all appearances cannot "
            "be listed. "
            "Open the index timeline to review candidates."
        )
    if stats := result.get("aggregation"):
        scope = (
            "In the current complete event index"
            if stats["exact_under_index"]
            else "In saved event observations (coverage or deduplication incomplete)"
        )
        text = (
            f"{scope}, {stats.get('label', stats['action'])} is grouped into "
            f"{stats['unique_events']} "
            f"events with {stats['evidence_clips']} evidence clips."
        )
        if stats["repeated_clips"]:
            text += f" {stats['repeated_clips']} repeated or replay clips are not counted twice."
        if stats["mode"] == "ranking":
            text += (
                " Counts for saved entity identifiers: "
                + (
                    "; ".join(
                        f"{row['entity']}: {row['events']} groups" for row in stats["ranking"]
                    )
                    or "none"
                )
                + "."
            )
            if not stats["ranking_complete"]:
                text += (
                    f" {stats['unknown_actors']} groups lack an entity or event coverage is "
                    "incomplete, so a whole-video leader cannot be confirmed."
                )
        if not stats["exact_under_index"]:
            text += (
                " These are observed groups, not whole-video totals; gaps may split the same event."
            )
        text += (
            " Event semantics and entity links come from "
            "saved records; the video was not re-analyzed."
        )
    if result.get("next_offset") is not None:
        text += " This answer shows the first page; load more in the index timeline."
    text += " No model calls in this turn."
    return Answer.model_validate(
        {
            "schema_version": "0.1",
            "status": "partial" if events else "no_result",
            "timebase": "video_absolute_seconds",
            "answer": text,
            "events": events,
            "unresolved": [
                "Based on saved observations; review coverage "
                "and unchecked ranges in the index timeline."
            ]
            if events
            else [],
            "followups": [],
            "errors": [],
            "trace": {
                "visible": {
                    "channels": [],
                    "segments_reviewed": coverage["reviewed_frames"],
                    "evidence_segments": len(
                        {e["frame_id"] for hit in events for e in hit["evidence"]}
                    ),
                },
                "debug": None,
            },
        }
    ).model_dump(mode="json")
