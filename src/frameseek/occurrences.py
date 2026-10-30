"""Offline, resumable visual observation index and exhaustive predicate search.

This module never decodes media or invokes a model. A trusted extractor supplies
actual source frame PTS and observations. A complete catalog is not a complete
recognition result: incomplete/uncertain inventories remain explicitly unknown.
"""

import hashlib
import json
import math
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, model_validator

from frameseek.schema import Contract, FiniteSeconds, Identifier
from frameseek.visual_search import IndexedFrame

Label = Annotated[str, Field(min_length=1, max_length=200)]


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CatalogFrame(IndexedFrame):
    ordinal: int = Field(ge=0, le=216000)
    # Only a fully enumerated decoded frame may claim its presentation interval.
    end: FiniteSeconds | None = None
    cut_before: bool = False


class Subject(Contract):
    # IDs are frame-local. Equal IDs in different frames do NOT establish identity.
    id: Identifier
    facts: dict[Label, list[Label]] = Field(max_length=64)
    # Supplied by a trusted extractor, never inferred from equal frame-local IDs.
    track_id: Identifier | None = None
    entity_id: Identifier | None = None
    # Explicit canonical event keys may link a replay to the original event.
    event_keys: dict[Label, Identifier] = Field(default_factory=dict, max_length=64)
    uncertain_fields: list[Label] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def bounded_facts(self):
        if any(not 1 <= len(values) <= 32 for values in self.facts.values()):
            raise ValueError("observation_fact_limit")
        event_keys = [normalized(k) for k in self.event_keys]
        if len(set(event_keys)) != len(event_keys) or any(not k for k in event_keys):
            raise ValueError("event_key_collision")
        keys = [normalized(k) for k in self.facts]
        if len(set(keys)) != len(keys) or any(not k for k in keys):
            raise ValueError("observation_field_collision")
        if any(not normalized(v) for values in self.facts.values() for v in values):
            raise ValueError("empty_observation_value")
        return self


class Observation(Contract):
    frame_id: Identifier
    subjects: list[Subject] = Field(max_length=100)
    # False includes a successful extractor that could only inspect some objects.
    inventory_complete: bool = False
    event_inventory_complete: bool = False

    @model_validator(mode="after")
    def unique_subjects(self):
        if len({s.id for s in self.subjects}) != len(self.subjects):
            raise ValueError("duplicate_frame_subject")
        return self


class Condition(Contract):
    field: Label
    value: Label
    operator: Literal["is", "is_not", "contains", "color_item"] = "is"

    @model_validator(mode="after")
    def nonempty(self):
        if not normalized(self.field) or not normalized(self.value):
            raise ValueError("empty_condition")
        if self.operator == "contains" and normalized(self.field) != "text":
            raise ValueError("contains_requires_visible_text")
        if self.operator == "color_item":
            from frameseek.semantic_search import COLOR_CONCEPTS, GARMENT_CONCEPTS

            parts = normalized(self.value).split()
            if (
                normalized(self.field) == "text"
                or not 2 <= len(parts) <= len(COLOR_CONCEPTS) + 1
                or len(set(parts[:-1])) != len(parts) - 1
                or not set(parts[:-1]) <= COLOR_CONCEPTS
                or parts[-1] not in GARMENT_CONCEPTS
            ):
                raise ValueError("invalid_color_item_condition")
        return self


class EventRule(Contract):
    """Data-driven state transition on the same tracked object, not image inference."""

    kind: Label
    before: list[Condition] = Field(min_length=1, max_length=12)
    after: list[Condition] = Field(min_length=1, max_length=12)


class ObservationIndex(Contract):
    format: Literal["frameseek_observations_v1"] = "frameseek_observations_v1"
    video: Identifier
    generation: int = Field(ge=1)
    duration: Annotated[float, Field(gt=0, le=3600, allow_inf_nan=False)]
    source_fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    extractor_revision: Label
    catalog_complete: bool = False
    event_rules: list[EventRule] = Field(default_factory=list, max_length=64)
    event_aliases: dict[Label, Label] = Field(default_factory=dict, max_length=1000)
    query_aliases: dict[Label, list[Condition]] = Field(default_factory=dict, max_length=1000)
    frames: list[CatalogFrame] = Field(min_length=1, max_length=216000)
    observations: list[Observation] = Field(default_factory=list, max_length=216000)

    @model_validator(mode="after")
    def validate_source(self):
        event_aliases = [normalized(alias) for alias in self.event_aliases]
        if len(set(event_aliases)) != len(event_aliases) or any(not a for a in event_aliases):
            raise ValueError("invalid_event_aliases")
        aliases = [normalized(alias) for alias in self.query_aliases]
        if (
            len(set(aliases)) != len(aliases)
            or any(not a for a in aliases)
            or any(not 1 <= len(conditions) <= 12 for conditions in self.query_aliases.values())
        ):
            raise ValueError("invalid_query_aliases")
        ids, times, ordinals, files = set(), set(), set(), set()
        previous = None
        for frame in self.frames:
            if (
                not re.fullmatch(r"[a-zA-Z0-9_-]+\.jpg", frame.file)
                or frame.frame_id in ids
                or frame.file in files
                or frame.t in times
                or frame.ordinal in ordinals
                or not frame.frame_id.startswith(self.video + "_")
                or frame.uri != f"/media/{self.video}/frames/{frame.file}"
                or frame.t >= self.duration
                or (frame.end is not None and not frame.t < frame.end <= self.duration)
                or (previous and (frame.t <= previous.t or frame.ordinal <= previous.ordinal))
            ):
                raise ValueError("invalid_observation_catalog")
            if self.catalog_complete and (
                frame.end is None
                or (previous is None and (frame.t != 0 or frame.ordinal != 0))
                or (previous and (frame.ordinal != previous.ordinal + 1 or frame.t != previous.end))
            ):
                raise ValueError("incomplete_source_catalog")
            if not self.catalog_complete and frame.end is not None:
                raise ValueError("sample_cannot_claim_interval")
            ids.add(frame.frame_id)
            files.add(frame.file)
            times.add(frame.t)
            ordinals.add(frame.ordinal)
            previous = frame
        if self.catalog_complete and previous.end != self.duration:
            raise ValueError("incomplete_source_catalog")
        observed = [o.frame_id for o in self.observations]
        if len(set(observed)) != len(observed) or not set(observed) <= ids:
            raise ValueError("foreign_or_duplicate_observation")
        return self

    def snapshot(self):
        return fingerprint(self.model_dump(mode="json"))


def pending_batches(index: ObservationIndex, size=8):
    """A complete plan, not top-k. Includes empty/failed gaps until checkpointed.

    Dispatch is deliberately absent. Unknown paid attempts must be reconciled by
    the existing ledger before an external worker uses this plan, never resent.
    """
    if type(size) is not int or not 1 <= size <= 8:
        raise ValueError("observation_batch_limit")
    done = {o.frame_id for o in index.observations}
    pending = [f.frame_id for f in index.frames if f.frame_id not in done]
    return [pending[i : i + size] for i in range(0, len(pending), size)]


def checkpoint(index: ObservationIndex, batch, expected_snapshot):
    """Immutable, generation-bound checkpoint; no replacement of completed work."""
    if expected_snapshot != index.snapshot():
        raise ValueError("observation_revision_conflict")
    observations = [Observation.model_validate(o) for o in batch]
    if not 1 <= len(observations) <= 8:
        raise ValueError("observation_batch_limit")
    allowed = {f.frame_id for f in index.frames}
    old = {o.frame_id: o for o in index.observations}
    for observation in observations:
        if observation.frame_id in old:
            if old[observation.frame_id] != observation:
                raise ValueError("observation_checkpoint_conflict")
        else:
            old[observation.frame_id] = observation
    data = index.model_dump(mode="json")
    data["observations"] = [old[f.frame_id].model_dump() for f in index.frames if f.frame_id in old]
    if any(o.frame_id not in allowed for o in observations):
        raise ValueError("foreign_observation")
    return ObservationIndex.model_validate(data)


def subject_verdict(subject, conditions):
    from frameseek.semantic_search import (
        BODY_CLOTHING_CONCEPTS,
        COLOR_CONCEPTS,
        GARMENT_CONCEPTS,
        canonical,
        interpret,
        primary_item_description,
    )

    raw_facts = {
        normalized(k): {normalized(v) for v in values} for k, values in subject.facts.items()
    }
    facts = {key: {canonical(value) for value in values} for key, values in raw_facts.items()}
    uncertain = {normalized(k) for k in subject.uncertain_fields}
    verdicts = []
    for condition in conditions:
        key, value = normalized(condition.field), canonical(condition.value)
        if key in uncertain or key not in facts:
            verdicts.append("unknown")
        else:
            if condition.operator == "contains":
                matched = any(
                    normalized(condition.value) in observed for observed in raw_facts[key]
                )
                verdicts.append("yes" if matched else "no")
            elif condition.operator == "color_item":
                *requested, item = normalized(condition.value).split()
                requested_colors = set(requested)
                matched = False
                ambiguous = False
                for observed in raw_facts[key]:
                    # A trim color is not necessarily the main item's color.
                    # Only remove this narrowly specified suffix, preserving the
                    # original observation and treating other descriptions as unknown.
                    words, problems = interpret(primary_item_description(observed))
                    if problems:
                        ambiguous = True
                        continue
                    colors = set(words) & COLOR_CONCEPTS
                    items = set(words) & GARMENT_CONCEPTS
                    item_matches = item in items or (
                        item == "clothing" and bool(items & BODY_CLOTHING_CONCEPTS)
                    )
                    if not problems and len(items) == 1 and set(words) == colors | items:
                        matched = requested_colors <= colors and item_matches
                        if matched:
                            break
                        if item_matches and requested_colors & colors:
                            ambiguous = True
                    elif requested_colors & colors and item_matches:
                        ambiguous = True
                if not matched and (
                    item in facts[key]
                    or item == "clothing"
                    and bool(facts[key] & BODY_CLOTHING_CONCEPTS)
                ):
                    color_key = key + "_color"
                    if (
                        len(requested_colors) != 1
                        or len(raw_facts[key]) != 1
                        or color_key in uncertain
                        or color_key not in facts
                        or len(raw_facts[color_key]) != 1
                    ):
                        ambiguous = True
                    elif requested_colors <= facts[color_key]:
                        matched = True
                verdicts.append("yes" if matched else "unknown" if ambiguous else "no")
            else:
                matched = value in facts[key]
                if key == "kind" and value == "person" and facts[key] & {"male", "female"}:
                    matched = True
                verdicts.append("yes" if matched == (condition.operator == "is") else "no")
    return "no" if "no" in verdicts else "unknown" if "unknown" in verdicts else "yes"


def search_occurrences(
    index: ObservationIndex,
    conditions,
    offset=0,
    limit=50,
    snapshot=None,
    time_window=None,
    candidate_offset=0,
    candidate_time=None,
    include_candidates=True,
):
    """Scan EVERY observation; paginate output only. No invented NL interpretation.

    Conditions must come from a separately grounded query compiler or explicit
    user filters. Unknown facts cannot satisfy negation. Adjacent hits form a
    visibility interval only for contiguous fully enumerated presentation frames,
    never across a cut, an unreviewed frame or an unknown result. These are query
    occurrences, not persistent person identities or counts of unique people.
    """
    conditions = [Condition.model_validate(c) for c in conditions]
    if not 1 <= len(conditions) <= 12:
        raise ValueError("observation_condition_limit")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("observation_page_limit")
    if type(candidate_offset) is not int or not 0 <= candidate_offset <= 216000:
        raise ValueError("observation_page_limit")
    if candidate_time is not None and (
        type(candidate_time) not in (int, float)
        or not math.isfinite(candidate_time)
        or not 0 <= candidate_time < index.duration
        or candidate_offset != 0
        or offset != 0
    ):
        raise ValueError("invalid_candidate_time")
    identity_data = {"index": index.snapshot(), "conditions": [c.model_dump() for c in conditions]}
    if time_window is not None:
        if (
            not isinstance(time_window, dict)
            or set(time_window) != {"start", "end"}
            or type(time_window["start"]) not in (int, float)
            or type(time_window["end"]) not in (int, float)
            or not 0 <= time_window["start"] < time_window["end"] <= index.duration
        ):
            raise ValueError("invalid_observation_time_window")
        identity_data["time_window"] = time_window
    identity = fingerprint(identity_data)
    if (snapshot is not None and snapshot != identity) or (
        (offset or candidate_offset or candidate_time is not None) and snapshot is None
    ):
        raise ValueError("observation_snapshot_conflict")
    observations = {o.frame_id: o for o in index.observations}
    occurrences, unknown, missing, candidates = [], [], [], []
    # Consecutive samples often contain identical attributes. Memoize only
    # within this search and cap the cache; never persist private observations.
    verdict_cache = {}

    def verdict(subject, condition_number=None):
        key = (
            tuple(sorted((field, tuple(values)) for field, values in subject.facts.items())),
            tuple(sorted(subject.uncertain_fields)),
            condition_number,
        )
        if key not in verdict_cache:
            selected = conditions if condition_number is None else [conditions[condition_number]]
            result = subject_verdict(subject, selected)
            if len(verdict_cache) < 4096:
                verdict_cache[key] = result
            return result
        return verdict_cache[key]

    last_hit = None
    reviewed = 0
    complete = index.catalog_complete and time_window is None
    scoped_frames = [
        frame
        for frame in index.frames
        if time_window is None
        or (
            frame.t < time_window["end"]
            and (
                frame.end > time_window["start"]
                if index.catalog_complete
                else frame.t >= time_window["start"]
            )
        )
    ]
    for frame in scoped_frames:
        observation = observations.get(frame.frame_id)
        if observation is None:
            missing.append(frame.frame_id)
            complete = False
            last_hit = None
            continue
        reviewed += 1
        subjects = [(s, verdict(s)) for s in observation.subjects]
        matches = [s.id for s, result in subjects if result == "yes"]
        ambiguous = any(result == "unknown" for _, result in subjects)
        # A lead is not an answer: one subject must satisfy at least one
        # condition and leave the rest unknown, without contradicting any.
        if include_candidates and not matches:
            if any(
                result == "unknown"
                and any(verdict(subject, i) == "yes" for i in range(len(conditions)))
                for subject, result in subjects
            ):
                candidates.append({"frame_id": frame.frame_id, "t": frame.t, "uri": frame.uri})
        if ambiguous or not observation.inventory_complete:
            unknown.append(frame.frame_id)
            complete = False
        if not matches:
            last_hit = None
            continue
        evidence = {"frame_id": frame.frame_id, "t": frame.t, "uri": frame.uri, "subjects": matches}
        # Even incomplete inventories may prove a positive hit, but they cannot
        # prove all matching objects were found in that frame.
        if (
            last_hit is not None
            and index.catalog_complete
            and not frame.cut_before
            and last_hit["end"] == frame.t
        ):
            last_hit["end"] = min(frame.end, time_window["end"]) if time_window else frame.end
            last_hit["evidence_count"] += 1
            if len(last_hit["evidence"]) == 1:
                last_hit["evidence"].append(evidence)
            else:
                last_hit["evidence"][-1] = evidence
        else:
            last_hit = {
                "start": max(frame.t, time_window["start"])
                if time_window and index.catalog_complete
                else frame.t,
                "end": min(frame.end, time_window["end"])
                if time_window and frame.end is not None
                else frame.end,
                "evidence": [evidence],
                "evidence_count": 1,
                "identity_continuity": False,
            }
            occurrences.append(last_hit)
    end = offset + limit
    if candidate_time is not None:
        candidate_offset = next(
            (i for i, frame in enumerate(candidates) if frame["t"] >= candidate_time),
            len(candidates),
        )
    return {
        "kind": "indexed_occurrences",
        "video_id": index.video,
        "generation": index.generation,
        "snapshot": identity,
        "occurrences": occurrences[offset:end],
        "total_occurrences": len(occurrences),
        "next_offset": end if end < len(occurrences) else None,
        "uncertain_candidates": candidates[candidate_offset : candidate_offset + 8]
        if offset == 0
        else [],
        "uncertain_candidate_total": len(candidates),
        "uncertain_candidate_next_offset": candidate_offset + 8
        if offset == 0 and candidate_offset + 8 < len(candidates)
        else None,
        "model_requests_sent": 0,
        "coverage": {
            "catalog_frames": len(scoped_frames),
            "reviewed_frames": reviewed,
            "missing_frame_ids": missing,
            "uncertain_frame_ids": unknown,
            "catalog_complete": index.catalog_complete,
            "all_indexed_observations_searched": time_window is None,
            "all_scope_observations_searched": True,
            "all_source_frames_reviewed": index.catalog_complete
            and not missing
            and time_window is None,
            "exhaustive_under_index": complete,
            "recognition_accuracy_verified": False,
            "time_scope": time_window,
        },
    }


def plan_source(directory, video, generation, metadata, extractor_revision):
    """Enumerate actual source presentation frames without visual inference.

    The bounded decoder runs in the existing OS sandbox. No JPEGs, source index
    or budgets are written. The plan is separate from legacy sparse ingestion.
    """
    from frameseek.media import MAX_UPLOAD, frame_timeline

    directory = directory.resolve()
    original = directory / "original"
    if original.is_symlink() or not original.is_file():
        raise ValueError("invalid_observation_source")
    before = original.stat()
    if before.st_size > MAX_UPLOAD:
        raise ValueError("observation_source_limit")
    digest = hashlib.sha256()
    with original.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    times = frame_timeline(directory, metadata)
    after = original.stat()
    if original.is_symlink() or (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("source_changed_during_plan")
    cuts = metadata.get("scene_boundaries", [])
    frames = []
    cut_index = 0
    for ordinal, t in enumerate(times):
        end = times[ordinal + 1] if ordinal + 1 < len(times) else metadata["duration"]
        cut = False
        while cut_index < len(cuts) and cuts[cut_index] <= t:
            cut = True
            cut_index += 1
        file = f"g{generation}_o{ordinal:06d}.jpg"
        frames.append(
            {
                "ordinal": ordinal,
                "t": t,
                "end": end,
                "cut_before": cut,
                "file": file,
                "frame_id": video + "_" + file[:-4],
                "uri": f"/media/{video}/frames/{file}",
                "caption": "Visual observations have not been extracted yet.",
            }
        )
    return ObservationIndex.model_validate(
        {
            "video": video,
            "generation": generation,
            "duration": metadata["duration"],
            "source_fingerprint": digest.hexdigest(),
            "extractor_revision": extractor_revision,
            "catalog_complete": True,
            "frames": frames,
        }
    )


MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def load_index(path, *, video, generation, source_fingerprint):
    """Trusted private path only; callers must perform ownership/expiry checks."""
    import os

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("observation_artifact_limit")
        index = ObservationIndex.model_validate_json(stream.read(MAX_ARTIFACT_BYTES + 1))
    if (index.video, index.generation, index.source_fingerprint) != (
        video,
        generation,
        source_fingerprint,
    ):
        raise ValueError("stale_observation_artifact")
    return index


def save_index(path, index, expected_snapshot=None):
    """Atomic private checkpoint with cross-process CAS, never overwrite blindly.

    This is an offline artifact store, not an approval or provider checkpoint.
    The existing attempt ledger remains the authority before any future dispatch.
    """
    import fcntl
    import os
    import tempfile
    from pathlib import Path

    index = ObservationIndex.model_validate(index.model_dump(mode="json"))
    data = index.model_dump_json().encode()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("observation_artifact_limit")
    path = Path(path)
    lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    temporary = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() or path.is_symlink():
            old = load_index(
                path,
                video=index.video,
                generation=index.generation,
                source_fingerprint=index.source_fingerprint,
            )
            if expected_snapshot is None or old.snapshot() != expected_snapshot:
                raise ValueError("observation_revision_conflict")
            if (
                old.extractor_revision != index.extractor_revision
                or old.frames != index.frames
                or old.duration != index.duration
                or old.catalog_complete != index.catalog_complete
                or old.event_rules != index.event_rules
                or old.event_aliases != index.event_aliases
            ):
                raise ValueError("observation_configuration_conflict")
            previous = {o.frame_id: o for o in old.observations}
            current = {o.frame_id: o for o in index.observations}
            if any(current.get(key) != observation for key, observation in previous.items()):
                raise ValueError("observation_checkpoint_conflict")
        elif expected_snapshot is not None:
            raise ValueError("observation_revision_conflict")
        descriptor, temporary = tempfile.mkstemp(prefix=".observation-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
        os.close(lock)


def main():
    """Explicit offline tool. No implicit runtime/key/approval/model loading."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--source-directory", type=Path, required=True)
    plan.add_argument("--metadata", type=Path, required=True)
    plan.add_argument("--video", required=True)
    plan.add_argument("--generation", type=int, required=True)
    plan.add_argument("--extractor-revision", required=True)
    plan.add_argument("--output", type=Path, required=True)
    update = commands.add_parser("checkpoint")
    update.add_argument("--index", type=Path, required=True)
    update.add_argument("--batch", type=Path, required=True)
    update.add_argument("--expected-snapshot", required=True)
    search = commands.add_parser("search")
    search.add_argument("--index", type=Path, required=True)
    search.add_argument("--conditions", type=Path, required=True)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--snapshot")
    args = parser.parse_args()

    def read(path):
        with path.open("rb") as stream:
            data = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("observation_artifact_limit")
        return json.loads(data)

    if args.command == "plan":
        index = plan_source(
            args.source_directory,
            args.video,
            args.generation,
            read(args.metadata),
            args.extractor_revision,
        )
        save_index(args.output, index)
    else:
        index = ObservationIndex.model_validate(read(args.index))
        if args.command == "search":
            print(
                json.dumps(
                    search_occurrences(
                        index, read(args.conditions), args.offset, args.limit, args.snapshot
                    ),
                    ensure_ascii=False,
                )
            )
            return
        index = checkpoint(index, read(args.batch), args.expected_snapshot)
        save_index(args.index, index, args.expected_snapshot)
    print(
        json.dumps(
            {
                "snapshot": index.snapshot(),
                "catalog_frames": len(index.frames),
                "reviewed_frames": len(index.observations),
                "pending_batches": len(pending_batches(index)),
                "model_requests_sent": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
