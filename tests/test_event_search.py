"""Temporal event reasoning on explicit annotations; NOT a visual recognizer."""

import copy

import pytest
from test_occurrences import catalog, observation

from frameseek.event_search import search_events
from frameseek.occurrences import ObservationIndex, checkpoint


def event_subject(track="track-A", actor="entity-A", key=None):
    return {
        "id": "local",
        "facts": {"event": ["lift"]},
        "track_id": track,
        "entity_id": actor,
        "event_keys": {"lift": key} if key else {},
    }


def event_index(hits=None, count=12):
    index = catalog(count)
    data = index.model_dump()
    data["event_aliases"] = {"搬运": "lift", "lifting": "lift"}
    index = ObservationIndex.model_validate(data)
    hits = (
        hits
        if hits is not None
        else {
            0: [event_subject()],
            1: [event_subject()],
            4: [event_subject()],
            5: [event_subject()],
        }
    )
    annotations = [
        observation(i, hits.get(i, [])) | {"event_inventory_complete": True} for i in range(count)
    ]
    for start in range(0, count, 8):
        index = checkpoint(index, annotations[start : start + 8], index.snapshot())
    return index


def test_continuous_frames_are_two_events_not_four_and_ranking_uses_entities():
    index = event_index()
    result = search_events(index, "搬运了多少次？")
    assert result["structured_conditions_evaluated"] is True
    assert result["verified_matches"] is False
    assert result["coverage"]["recognition_accuracy_verified"] is False
    assert result["aggregation"]["unique_events"] == 2
    assert [(c["start"], c["end"]) for c in result["occurrences"]] == [(0, 2), (4, 6)]
    assert result["aggregation"]["exact_under_index"]
    ranking = search_events(index, "谁搬运最多？")
    assert ranking["aggregation"]["ranking"] == [{"entity": "entity-A", "events": 2}]
    assert ranking["aggregation"]["ranking_complete"]
    assert result["model_requests_sent"] == 0


def test_confirmed_replay_key_deduplicates_without_merging_separate_timeline_clips():
    hits = {
        0: [event_subject(key="event-one")],
        1: [event_subject(key="event-one")],
        4: [event_subject(key="event-one")],
        7: [event_subject(key="event-two")],
    }
    result = search_events(event_index(hits), "how many lifting events?")
    assert result["aggregation"]["unique_events"] == 2
    assert result["aggregation"]["evidence_clips"] == 3
    assert result["aggregation"]["repeated_clips"] == 1
    assert result["total_occurrences"] == 3
    assert result["aggregation"]["exact_under_index"]


def test_frame_local_ids_and_similar_appearance_never_prove_identity():
    subject = event_subject(None, None)
    result = search_events(event_index({0: [subject], 1: [subject]}), "count lift")
    assert result["aggregation"]["unique_events"] == 0
    assert not result["aggregation"]["exact_under_index"]
    assert result["coverage"]["unbound_event_frame_ids"]
    # A track is not a confirmed source-wide entity identity for rankings.
    result = search_events(event_index({0: [event_subject(actor=None)]}), "谁搬运最多")
    assert result["aggregation"]["unknown_actors"] == 1
    assert not result["aggregation"]["ranking_complete"]


def test_gap_cut_and_partial_event_inventory_invalidate_full_totals():
    index = event_index()
    data = index.model_dump()
    data["observations"] = [o for o in data["observations"] if o["frame_id"] != "vid_offline_f1"]
    result = search_events(ObservationIndex.model_validate(data), "count lift")
    assert not result["aggregation"]["exact_under_index"]
    data = index.model_dump()
    data["frames"][1]["cut_before"] = True
    result = search_events(ObservationIndex.model_validate(data), "count lift")
    assert len(result["occurrences"]) == 3
    assert not result["aggregation"]["exact_under_index"]
    data = index.model_dump()
    data["observations"][3]["event_inventory_complete"] = False
    assert not search_events(ObservationIndex.model_validate(data), "count lift")["aggregation"][
        "exact_under_index"
    ]


def test_other_domains_are_data_driven_and_unknown_qualifiers_are_not_dropped():
    data = event_index().model_dump()
    data["event_aliases"] = {"开门": "door_open"}
    for observation_ in data["observations"]:
        for subject in observation_["subjects"]:
            subject["facts"] = {"event": ["door_open"]}
    index = ObservationIndex.model_validate(data)
    assert search_events(index, "开门了多少次")["aggregation"]["unique_events"] == 2
    assert search_events(index, "红色开门多少次")["kind"] == "clarification_required"
    assert search_events(index, "count lift")["kind"] == "clarification_required"


def test_pagination_uses_immutable_snapshot_and_keeps_all_events_in_statistics():
    index = event_index()
    result = search_events(index, "count lift", limit=1)
    assert len(result["occurrences"]) == 1 and result["aggregation"]["unique_events"] == 2
    second = search_events(index, "count lift", offset=1, limit=1, snapshot=result["snapshot"])
    assert second["occurrences"][0]["start"] == 4 and second["next_offset"] is None
    with pytest.raises(ValueError, match="snapshot"):
        search_events(index, "count lift", offset=1)
    data = copy.deepcopy(index.model_dump())
    data["observations"][2]["event_inventory_complete"] = False
    with pytest.raises(ValueError, match="snapshot"):
        search_events(
            ObservationIndex.model_validate(data), "count lift", snapshot=result["snapshot"]
        )


def test_conflicting_event_actors_and_ambiguous_tracks_fail_closed():
    hits = {0: [event_subject(key="one")], 3: [event_subject(actor="different", key="one")]}
    with pytest.raises(ValueError, match="identity_conflict"):
        search_events(event_index(hits), "count lift")
    hits = {0: [event_subject(), event_subject() | {"id": "local-B"}]}
    with pytest.raises(ValueError, match="ambiguous_event_track"):
        search_events(event_index(hits), "count lift")


def test_generic_state_transition_recognition_counts_edges_not_static_frames():
    data = event_index(hits={}).model_dump()
    data["event_rules"] = [
        {
            "kind": "door_open",
            "before": [{"field": "state", "value": "closed"}],
            "after": [{"field": "state", "value": "open"}],
        }
    ]
    data["event_aliases"] = {"开门": "door_open"}
    for i, row in enumerate(data["observations"]):
        row["subjects"] = [
            {
                "id": "local",
                "track_id": "door-1",
                "entity_id": "door-entity",
                "facts": {"state": ["open" if i in (2, 3, 4, 8, 9) else "closed"]},
            }
        ]
    index = ObservationIndex.model_validate(data)
    result = search_events(index, "开门多少次")
    assert result["aggregation"]["unique_events"] == 2
    assert [h["start"] for h in result["occurrences"]] == [2, 8]
    assert result["aggregation"]["exact_under_index"]
    # Unknown/cut before-state cannot constitute a proven opening event.
    data["observations"][1]["subjects"][0]["uncertain_fields"] = ["state"]
    data["frames"][8]["cut_before"] = True
    result = search_events(ObservationIndex.model_validate(data), "开门多少次")
    assert result["aggregation"]["unique_events"] == 0
    assert not result["aggregation"]["exact_under_index"]
