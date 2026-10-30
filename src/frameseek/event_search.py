"""Zero-dispatch event episodes over trusted per-frame action observations.

This is temporal reasoning, not pixel recognition. It never promotes a caption,
score change, similar clothing or frame-local subject ID into a completed event.
"""

import re
from collections import Counter

from frameseek.occurrences import Subject, fingerprint, normalized, subject_verdict

# Query grammar only. Event names/aliases come from this source's observations.
GRAMMAR = (
    "整个比赛",
    "整场比赛",
    "整段视频",
    "视频里面",
    "视频里",
    "这段视频",
    "事件",
    "总共",
    "一共",
    "总次数",
    "多少次",
    "几次",
    "多少个",
    "几个",
    "总数",
    "次数",
    "排行榜",
    "排名",
    "最多",
    "最少",
    "分别",
    "谁",
    "哪个",
    "有",
    "了",
    "的",
    "请",
    "帮我",
    "how many",
    "how often",
    "in the video",
    "events",
    "event",
    "count",
    "total",
    "rank",
    "who",
    "most",
    "least",
    "the",
    "all",
    "please",
)


def event_names(index):
    names = {
        normalized(action)
        for observation in index.observations
        for subject in observation.subjects
        for key, values in subject.facts.items()
        if normalized(key) == "event"
        for action in values
    }
    return names | {normalized(rule.kind) for rule in index.event_rules}


def resolve_event(index, question):
    text = normalized(question)
    aliases = {name: name for name in event_names(index)}
    aliases.update({normalized(a): normalized(b) for a, b in index.event_aliases.items()})
    found = set()
    for alias in sorted(aliases, key=len, reverse=True):
        pattern = re.escape(alias)
        if re.fullmatch(r"[a-z0-9 _-]+", alias):
            pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
        if re.search(pattern, text):
            found.add(aliases[alias])
            text = re.sub(pattern, " ", text)
    for phrase in sorted(GRAMMAR, key=len, reverse=True):
        if re.fullmatch(r"[a-z ]+", phrase):
            text = re.sub(r"\b" + re.escape(phrase) + r"\b", " ", text)
        else:
            text = text.replace(phrase, " ")
    residual = re.sub(r"[\W_]+", "", text)
    if len(found) != 1 or residual:
        return None
    return next(iter(found))


def transition_marks(index, action):
    """Recognize configured state changes, requiring adjacent observed source frames.

    A rising after-state plus the required before-state constitutes one event.
    Unknown facts/identity, cuts, sparse samples and observation gaps cannot bridge
    an event. No new observation of pixels is performed here.
    """
    rules = [r for r in index.event_rules if normalized(r.kind) == action]
    observations = {o.frame_id: o for o in index.observations}
    marks, unknown = {}, set()
    previous = None
    for frame in index.frames:
        observation = observations.get(frame.frame_id)
        if observation is None:
            previous = None
            continue
        current = {}
        for subject in observation.subjects:
            if subject.track_id:
                if subject.track_id in current:
                    raise ValueError("ambiguous_event_track")
                current[subject.track_id] = subject
            elif rules:
                unknown.add(frame.frame_id)
        if rules and previous and index.catalog_complete and not frame.cut_before:
            old_frame, old_subjects = previous
            if old_frame.end == frame.t:
                for track, subject in current.items():
                    prior = old_subjects.get(track)
                    if prior is None:
                        # A newly observed track has no proven before-state.
                        if any(subject_verdict(subject, r.after) != "no" for r in rules):
                            unknown.add(frame.frame_id)
                        continue
                    for rule in rules:
                        verdicts = (
                            subject_verdict(prior, rule.before),
                            subject_verdict(prior, rule.after),
                            subject_verdict(subject, rule.after),
                        )
                        if "unknown" in verdicts:
                            unknown.add(frame.frame_id)
                        if verdicts != ("yes", "no", "yes"):
                            continue
                        if prior.entity_id != subject.entity_id:
                            raise ValueError("event_identity_conflict")
                        if any(
                            normalized(k) == "event" and action in map(normalized, vals)
                            for k, vals in subject.facts.items()
                        ):
                            continue  # An explicit mark already represents this transition.
                        key = fingerprint(
                            {"rule": rule.model_dump(), "track": track, "frame": frame.frame_id}
                        )
                        marks.setdefault(frame.frame_id, []).append(
                            Subject(
                                id="derived_" + key[:24],
                                facts={"event": [action]},
                                track_id=track,
                                entity_id=subject.entity_id,
                                event_keys={action: "transition_" + key[:32]},
                            )
                        )
        elif rules and (frame.cut_before or not index.catalog_complete):
            unknown.add(frame.frame_id)
        elif rules and any(
            subject_verdict(s, r.after) != "no" for s in current.values() for r in rules
        ):
            unknown.add(frame.frame_id)
        previous = frame, current
    return marks, unknown


def search_events(index, question, offset=0, limit=40, snapshot=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("event_page_limit")
    action = resolve_event(index, question)
    if action is None:
        return {
            "kind": "clarification_required",
            "message": "The event index cannot interpret this condition. Available event types: "
            + (", ".join(sorted(event_names(index))) or "none")
            + ". No model was called.",
            "available_conditions": [],
            "model_requests_sent": 0,
        }
    ranking = bool(re.search(r"谁|哪个|排名|排行榜|最多|最少|\bwho\b|\brank\b", question, re.I))
    identity = fingerprint({"index": index.snapshot(), "event": action, "ranking": ranking})
    if (offset and snapshot is None) or snapshot is not None and snapshot != identity:
        raise ValueError("event_snapshot_conflict")
    observations = {o.frame_id: o for o in index.observations}
    derived, rule_unknown = transition_marks(index, action)
    groups, active, clips = {}, {}, []
    missing, uncertain, unbound = [], list(rule_unknown), []
    reviewed = 0
    for frame in index.frames:
        observation = observations.get(frame.frame_id)
        if observation is None:
            missing.append(frame.frame_id)
            active = {}
            continue
        reviewed += 1
        if not observation.event_inventory_complete:
            uncertain.append(frame.frame_id)
        next_active = {}
        frame_tracks = set()
        for subject in [*observation.subjects, *derived.get(frame.frame_id, [])]:
            facts = {
                normalized(k): {normalized(v) for v in vals} for k, vals in subject.facts.items()
            }
            if "event" in {normalized(k) for k in subject.uncertain_fields}:
                uncertain.append(frame.frame_id)
                continue
            if action not in facts.get("event", set()):
                continue
            canonical = {normalized(k): v for k, v in subject.event_keys.items()}.get(action)
            track = subject.track_id
            if not canonical and not track:
                unbound.append(frame.frame_id)
                continue
            if track and track in frame_tracks:
                raise ValueError("ambiguous_event_track")
            if track:
                frame_tracks.add(track)
            prior = active.get(track) if track and not frame.cut_before else None
            contiguous = bool(index.catalog_complete and prior and prior["end"] == frame.t)
            # Equal IDs only come from explicit event keys; mere visual similarity
            # cannot deduplicate different plays or bridge cuts/unknown frames.
            key = (
                f"canonical:{canonical}"
                if canonical
                else (prior["key"] if contiguous else f"episode:{frame.frame_id}:{track}")
            )
            group = groups.setdefault(key, {"actor": subject.entity_id, "key": key})
            if group["actor"] != subject.entity_id:
                raise ValueError("event_identity_conflict")
            evidence = {"frame_id": frame.frame_id, "t": frame.t, "uri": frame.uri}
            if contiguous and prior["key"] == key:
                clip = prior["clip"]
                clip["end"] = frame.end
                clip["evidence_count"] += 1
                if len(clip["evidence"]) == 1:
                    clip["evidence"].append(evidence)
                else:
                    clip["evidence"][-1] = evidence
            else:
                clip = {
                    "start": frame.t,
                    "end": frame.end,
                    "evidence": [evidence],
                    "evidence_count": 1,
                    "event_key": key,
                    "actor": subject.entity_id,
                    "identity_continuity": bool(subject.entity_id),
                }
                clips.append(clip)
            if track:
                next_active[track] = {"end": frame.end, "key": key, "clip": clip}
        active = next_active
    counts = Counter(g["actor"] for g in groups.values() if g["actor"] is not None)
    unknown_actors = sum(g["actor"] is None for g in groups.values())
    unresolved_links = bool(
        any(f.cut_before for f in index.frames) and any(k.startswith("episode:") for k in groups)
    )
    complete = bool(
        index.catalog_complete
        and not missing
        and not uncertain
        and not unbound
        and not unresolved_links
    )
    page_end = offset + limit
    return {
        "kind": "indexed_occurrences",
        "structured_conditions_evaluated": True,
        "verified_matches": False,
        "video_id": index.video,
        "generation": index.generation,
        "snapshot": identity,
        "occurrences": clips[offset:page_end],
        "total_occurrences": len(clips),
        "next_offset": page_end if page_end < len(clips) else None,
        "model_requests_sent": 0,
        "available_conditions": [],
        "aggregation": {
            "action": action,
            "label": next(
                (
                    a
                    for a, target in index.event_aliases.items()
                    if normalized(target) == action and normalized(a) in normalized(question)
                ),
                action,
            ),
            "mode": "ranking" if ranking else "count",
            "unique_events": len(groups),
            "evidence_clips": len(clips),
            "repeated_clips": len(clips) - len(groups),
            "ranking": [
                {"entity": actor, "events": count}
                for actor, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            ],
            "unknown_actors": unknown_actors,
            "exact_under_index": complete,
            "ranking_complete": complete and not unknown_actors,
        },
        "coverage": {
            "catalog_frames": len(index.frames),
            "reviewed_frames": reviewed,
            "missing_frame_ids": missing,
            "uncertain_frame_ids": sorted(set(uncertain)),
            "unbound_event_frame_ids": unbound,
            "catalog_complete": index.catalog_complete,
            "all_indexed_observations_searched": True,
            "all_source_frames_reviewed": index.catalog_complete and not missing,
            "exhaustive_under_index": complete,
            "recognition_accuracy_verified": False,
        },
    }
