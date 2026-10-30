"""Offline extractor contracts only: no models, media or operational budgets."""

import copy

import pytest

from frameseek.occurrences import (
    ObservationIndex,
    checkpoint,
    pending_batches,
    search_occurrences,
)


def catalog(count=24, complete=True):
    return ObservationIndex.model_validate(
        {
            "video": "vid_offline",
            "generation": 3,
            "duration": float(count),
            "source_fingerprint": "a" * 64,
            "extractor_revision": "synthetic-fixture-v1",
            "catalog_complete": complete,
            "frames": [
                {
                    "ordinal": i,
                    "frame_id": f"vid_offline_f{i}",
                    "file": f"f{i}.jpg",
                    "uri": f"/media/vid_offline/frames/f{i}.jpg",
                    "caption": "synthetic",
                    "t": float(i),
                    "end": float(i + 1) if complete else None,
                }
                for i in range(count)
            ],
        }
    )


def observation(i, subjects=None, complete=True):
    return {
        "frame_id": f"vid_offline_f{i}",
        "inventory_complete": complete,
        "subjects": subjects
        if subjects is not None
        else [
            {"id": "local_A", "facts": {"kind": ["person"], "hat": ["red"], "clothing": ["white"]}}
        ],
    }


CRITERIA = [{"field": "hat", "value": "red"}, {"field": "clothing", "value": "white"}]


def populated(count=24):
    index = catalog(count)
    for batch in pending_batches(index):
        index = checkpoint(
            index, [observation(int(f.rsplit("f", 1)[1])) for f in batch], index.snapshot()
        )
    return index


def test_full_plan_and_resume_do_not_repeat_completed_extraction():
    index = catalog(25)
    assert list(map(len, pending_batches(index))) == [8, 8, 8, 1]
    old = index.snapshot()
    index = checkpoint(index, [observation(i) for i in range(8)], old)
    # Persist/reload an artifact; resumed work excludes the entire completed batch.
    index = ObservationIndex.model_validate_json(index.model_dump_json())
    assert len(pending_batches(index)) == 3
    assert pending_batches(index)[0][0] == "vid_offline_f8"
    with pytest.raises(ValueError, match="revision"):
        checkpoint(index, [observation(8)], old)
    assert checkpoint(index, [observation(0)], index.snapshot()) == index
    changed = observation(0, subjects=[])
    with pytest.raises(ValueError, match="checkpoint_conflict"):
        checkpoint(index, [changed], index.snapshot())


def test_all_frames_searched_not_top_eight_and_contiguous_timeline():
    result = search_occurrences(populated(), CRITERIA)
    assert result["total_occurrences"] == 1
    hit = result["occurrences"][0]
    assert (hit["start"], hit["end"]) == (0, 24)
    assert result["coverage"]["reviewed_frames"] == 24
    assert result["coverage"]["exhaustive_under_index"] is True
    assert result["coverage"]["recognition_accuracy_verified"] is False
    assert result["model_requests_sent"] == 0


def test_time_scoped_search_stays_within_requested_bounds_and_snapshot():
    index = populated()
    scope = {"start": 4, "end": 6}
    result = search_occurrences(index, CRITERIA, time_window=scope)
    assert result["coverage"]["time_scope"] == scope
    assert not result["coverage"]["all_source_frames_reviewed"]
    assert [(hit["start"], hit["end"]) for hit in result["occurrences"]] == [(4, 6)]
    with pytest.raises(ValueError, match="observation_snapshot_conflict"):
        search_occurrences(
            index, CRITERIA, snapshot=result["snapshot"], time_window={"start": 5, "end": 6}
        )


def test_short_scope_partial_subject_candidates_are_not_matching_evidence():
    index = catalog(4, complete=False)
    index = checkpoint(
        index,
        [
            observation(
                0,
                [
                    {
                        "id": "partial",
                        "facts": {"kind": ["person"], "garment": ["jersey"], "color": ["white"]},
                    }
                ],
            ),
            observation(1, [{"id": "other", "facts": {"kind": ["car"], "garment": ["jersey"]}}]),
            observation(
                2, [{"id": "hit", "facts": {"kind": ["person"], "garment": ["white jersey"]}}]
            ),
            observation(3, [{"id": "unknown", "facts": {"pose": ["standing"]}}]),
        ],
        index.snapshot(),
    )
    conditions = [
        {"field": "kind", "value": "person"},
        {"field": "garment", "value": "white jersey", "operator": "color_item"},
    ]
    result = search_occurrences(index, conditions, time_window={"start": 0, "end": 4})
    assert result["total_occurrences"] == 1
    assert [item["frame_id"] for item in result["uncertain_candidates"]] == ["vid_offline_f0"]
    assert result["uncertain_candidate_total"] == 1
    assert result["occurrences"][0]["evidence"][0]["frame_id"] == "vid_offline_f2"
    assert search_occurrences(index, conditions)["uncertain_candidate_total"] == 1
    assert result["model_requests_sent"] == 0


def test_full_video_candidates_paginate_without_promoting_or_changing_snapshot():
    index = catalog(20, complete=False)
    for start in range(0, 20, 8):
        rows = [
            observation(i, [{"id": "person", "facts": {"kind": ["person"], "garment": ["jersey"]}}])
            for i in range(start, min(start + 8, 20))
        ]
        index = checkpoint(index, rows, index.snapshot())
    conditions = [
        {"field": "kind", "value": "person"},
        {"field": "garment", "value": "white jersey", "operator": "color_item"},
    ]
    first = search_occurrences(index, conditions)
    assert first["total_occurrences"] == 0 and first["uncertain_candidate_total"] == 20
    assert len(first["uncertain_candidates"]) == 8 and first["uncertain_candidate_next_offset"] == 8
    second = search_occurrences(index, conditions, snapshot=first["snapshot"], candidate_offset=8)
    assert [frame["t"] for frame in second["uncertain_candidates"]] == list(range(8, 16))
    jumped = search_occurrences(index, conditions, snapshot=first["snapshot"], candidate_time=15.5)
    assert [frame["t"] for frame in jumped["uncertain_candidates"]] == list(range(16, 20))
    assert jumped["uncertain_candidate_next_offset"] is None
    with pytest.raises(ValueError, match="invalid_candidate_time"):
        search_occurrences(index, conditions, snapshot=first["snapshot"], candidate_time=20)
    with pytest.raises(ValueError, match="observation_snapshot_conflict"):
        search_occurrences(index, conditions, candidate_time=15)
    with pytest.raises(ValueError, match="observation_snapshot_conflict"):
        search_occurrences(index, conditions, candidate_offset=8)
    with pytest.raises(ValueError, match="observation_snapshot_conflict"):
        search_occurrences(
            index,
            conditions + [{"field": "kind", "value": "person"}],
            snapshot=first["snapshot"],
            candidate_offset=8,
        )


def test_search_reuses_verdicts_without_merging_subject_identity_or_uncertainty(monkeypatch):
    import frameseek.occurrences as occurrences

    index = catalog(16, complete=False)
    for start in (0, 8):
        rows = [
            observation(i, [{"id": f"person-{i}", "facts": {"kind": ["person"]}}])
            for i in range(start, start + 8)
        ]
        rows[-1]["subjects"][0]["uncertain_fields"] = ["kind"]
        index = checkpoint(index, rows, index.snapshot())
    original = occurrences.subject_verdict
    calls = []

    def counted(subject, conditions):
        calls.append(1)
        return original(subject, conditions)

    monkeypatch.setattr(occurrences, "subject_verdict", counted)
    result = search_occurrences(index, [{"field": "kind", "value": "person"}])
    assert result["total_occurrences"] == 14
    assert result["coverage"]["uncertain_frame_ids"] == ["vid_offline_f7", "vid_offline_f15"]
    assert result["occurrences"][0]["evidence"][0]["subjects"] == ["person-0"]
    assert result["occurrences"][1]["evidence"][0]["subjects"] == ["person-1"]
    assert len(calls) <= 3


def test_full_frame_overlapping_scope_start_is_found_without_fabricating_timestamp():
    from frameseek.index_service import chat_answer

    index = populated()
    result = search_occurrences(index, CRITERIA, time_window={"start": 4.25, "end": 4.75})
    assert result["coverage"]["reviewed_frames"] == 1
    hit = result["occurrences"][0]
    assert (hit["start"], hit["end"]) == (4.25, 4.75)
    assert hit["evidence"][0]["t"] == 4
    result.update(duration=index.duration, question="fixture")
    answer = chat_answer(result)
    assert answer["events"][0]["start"] == 4
    assert answer["events"][0]["evidence"][0]["t"] == 4
    assert "first frame appears before the selected range" in answer["events"][0]["summary"]

    sampled = catalog(2, complete=False)
    sampled = checkpoint(sampled, [observation(0), observation(1)], sampled.snapshot())
    assert not search_occurrences(sampled, CRITERIA, time_window={"start": 0.25, "end": 0.75})[
        "occurrences"
    ]  # A sample alone cannot establish a continuous interval.


def test_brief_appearance_after_top_eight_and_end_of_video_retained():
    index = catalog()
    observations = [observation(i, subjects=[] if i not in (13, 23) else None) for i in range(24)]
    for start in range(0, 24, 8):
        index = checkpoint(index, observations[start : start + 8], index.snapshot())
    result = search_occurrences(index, CRITERIA)
    assert [(h["start"], h["end"]) for h in result["occurrences"]] == [(13, 14), (23, 24)]


def test_cuts_unreviewed_and_uncertain_frames_split_occurrences():
    data = catalog(8).model_dump()
    data["frames"][2]["cut_before"] = True
    index = ObservationIndex.model_validate(data)
    rows = [observation(i) for i in (0, 1, 2, 4, 5, 6, 7)]
    rows[4]["subjects"][0]["uncertain_fields"] = ["hat"]  # frame5
    index = checkpoint(index, rows, index.snapshot())
    result = search_occurrences(index, CRITERIA)
    assert [(h["start"], h["end"]) for h in result["occurrences"]] == [
        (0, 2),
        (2, 3),
        (4, 5),
        (6, 8),
    ]
    assert result["coverage"]["missing_frame_ids"] == ["vid_offline_f3"]
    assert result["coverage"]["uncertain_frame_ids"] == ["vid_offline_f5"]
    assert not result["coverage"]["exhaustive_under_index"]


def test_conditions_cannot_be_combined_across_different_subjects():
    rows = [
        observation(
            0,
            subjects=[
                {"id": "A", "facts": {"hat": ["red"], "clothing": ["blue"]}},
                {"id": "B", "facts": {"hat": ["blue"], "clothing": ["white"]}},
            ],
        )
    ]
    index = checkpoint(catalog(1), rows, catalog(1).snapshot())
    assert search_occurrences(index, CRITERIA)["occurrences"] == []
    assert search_occurrences(index, CRITERIA)["coverage"]["exhaustive_under_index"]


@pytest.mark.parametrize(
    "field,value",
    [("vehicle", "bicycle"), ("material", "copper"), ("动作", "举起包裹"), ("颜色", "黄色")],
)
def test_same_engine_works_for_arbitrary_fields_and_values(field, value):
    index = catalog(1)
    index = checkpoint(
        index,
        [observation(0, subjects=[{"id": "item", "facts": {field: [value]}}])],
        index.snapshot(),
    )
    assert search_occurrences(index, [{"field": field, "value": value}])["total_occurrences"] == 1


def test_absent_fields_do_not_satisfy_negation_or_full_absence():
    index = catalog(1)
    index = checkpoint(
        index,
        [observation(0, subjects=[{"id": "A", "facts": {"kind": ["person"]}}])],
        index.snapshot(),
    )
    result = search_occurrences(index, [{"field": "hat", "value": "red", "operator": "is_not"}])
    assert not result["occurrences"]
    assert not result["coverage"]["exhaustive_under_index"]


def test_sparse_catalog_never_becomes_intervals_or_full_coverage():
    index = catalog(2, complete=False)
    index = checkpoint(index, [observation(0), observation(1)], index.snapshot())
    result = search_occurrences(index, CRITERIA)
    assert len(result["occurrences"]) == 2
    assert all(h["end"] is None for h in result["occurrences"])
    assert not result["coverage"]["all_source_frames_reviewed"]
    assert not result["coverage"]["exhaustive_under_index"]


def test_pagination_returns_every_occurrence_and_binds_query_and_revision():
    data = populated().model_dump()
    for frame in data["frames"]:
        frame["cut_before"] = True
    index = ObservationIndex.model_validate(data)
    first = search_occurrences(index, CRITERIA, limit=5)
    results = first["occurrences"][:]
    offset = first["next_offset"]
    while offset is not None:
        page = search_occurrences(index, CRITERIA, offset, 5, first["snapshot"])
        results += page["occurrences"]
        offset = page["next_offset"]
    assert [h["start"] for h in results] == list(range(24))
    with pytest.raises(ValueError, match="snapshot"):
        search_occurrences(index, CRITERIA, offset=5)
    with pytest.raises(ValueError, match="snapshot"):
        search_occurrences(
            index, [{"field": "hat", "value": "blue"}], 5, snapshot=first["snapshot"]
        )
    changed = index.model_copy(update={"generation": 4})
    with pytest.raises(ValueError, match="snapshot"):
        search_occurrences(changed, CRITERIA, 5, snapshot=first["snapshot"])


@pytest.mark.parametrize("change", ["gap", "foreign", "duplicate", "interval", "empty_field"])
def test_bad_catalog_and_observations_fail_closed(change):
    data = catalog(2).model_dump()
    if change == "gap":
        data["frames"][1]["t"] = 1.1
    if change == "foreign":
        data["frames"][0]["uri"] = "/media/other/frames/f0.jpg"
    if change == "duplicate":
        data["frames"][1]["frame_id"] = data["frames"][0]["frame_id"]
    if change == "interval":
        data["catalog_complete"] = False
    if change == "empty_field":
        row = observation(0)
        row["subjects"][0]["facts"] = {" ": ["x"]}
        data["observations"] = [row]
    with pytest.raises(ValueError):
        ObservationIndex.model_validate(data)


def test_unknown_foreign_or_conflicting_checkpoint_cannot_publish():
    index = catalog(2)
    foreign = observation(9)
    with pytest.raises(ValueError, match="foreign"):
        checkpoint(index, [foreign], index.snapshot())
    duplicate = observation(0)
    altered = copy.deepcopy(duplicate)
    altered["subjects"] = []
    with pytest.raises(ValueError, match="conflict"):
        checkpoint(index, [duplicate, altered], index.snapshot())


def test_private_atomic_artifact_reuse_and_stale_writer_rejection(tmp_path):
    from frameseek.occurrences import load_index, save_index

    path = tmp_path / "observations.json"
    original = catalog(2)
    save_index(path, original)
    restored = load_index(path, video="vid_offline", generation=3, source_fingerprint="a" * 64)
    assert restored == original
    assert path.stat().st_mode & 0o777 == 0o600
    updated = checkpoint(original, [observation(0)], original.snapshot())
    save_index(path, updated, original.snapshot())
    with pytest.raises(ValueError, match="revision"):
        save_index(path, original, original.snapshot())
    with pytest.raises(ValueError, match="checkpoint"):
        save_index(path, original, updated.snapshot())
    with pytest.raises(ValueError, match="stale"):
        load_index(path, video="other", generation=3, source_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="stale"):
        load_index(path, video="vid_offline", generation=4, source_fingerprint="a" * 64)
    symlink = tmp_path / "link.json"
    symlink.symlink_to(path)
    with pytest.raises(OSError):
        load_index(symlink, video="vid_offline", generation=3, source_fingerprint="a" * 64)


def test_full_plan_uses_actual_vfr_pts_and_keeps_every_brief_frame(tmp_path, monkeypatch):
    import json

    import frameseek.media as media
    from frameseek.occurrences import plan_source

    calls = []

    def run(directory, args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(
            {
                "frames": [
                    {"best_effort_timestamp_time": str(t), "pkt_duration_time": "0.2"}
                    for t in (2, 2.04, 2.081, 2.2)
                ]
            }
        ), ""

    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(media.shutil, "which", lambda name: "/trusted/ffprobe")
    (tmp_path / "original").write_bytes(b"synthetic-source")
    index = plan_source(
        tmp_path,
        "vid_offline",
        3,
        {"duration": 0.4, "start_pts": 2.0, "scene_boundaries": [0.1]},
        "offline-test",
    )
    assert len(index.frames) == 4
    assert index.frames[1].t == pytest.approx(0.04)
    assert index.frames[2].end == pytest.approx(0.2)
    assert index.frames[3].cut_before
    assert len(pending_batches(index)[0]) == 4
    assert not index.observations
    assert not (tmp_path / "frames").exists()
    assert calls[0][1]["output_limit"] == 32 * 1024 * 1024


@pytest.mark.parametrize("pts", [[0, 0.1, 0.1], [0, 0.2, 0.1], [0.1, 0.2], [0, float("nan")]])
def test_incomplete_or_nonmonotonic_pts_cannot_claim_complete_catalog(tmp_path, monkeypatch, pts):
    import json

    import frameseek.media as media

    monkeypatch.setattr(media.shutil, "which", lambda name: "/trusted/ffprobe")
    monkeypatch.setattr(
        media,
        "run",
        lambda *a, **k: (
            json.dumps({"frames": [{"best_effort_timestamp_time": str(t)} for t in pts]}),
            "",
        ),
    )
    with pytest.raises(media.MediaError):
        media.frame_timeline(tmp_path, {"duration": 1.0, "start_pts": 0.0})


def test_large_contiguous_match_has_bounded_evidence_but_searches_all_frames():
    index = populated(120)
    result = search_occurrences(index, CRITERIA)
    hit = result["occurrences"][0]
    assert hit["end"] == 120 and hit["evidence_count"] == 120
    assert len(hit["evidence"]) == 2
    assert hit["evidence"][-1]["t"] == 119
    assert hit["identity_continuity"] is False


def test_offline_command_searches_persisted_index_without_runtime_or_provider(tmp_path):
    import json
    import subprocess
    import sys

    from frameseek.occurrences import save_index

    path = tmp_path / "index.json"
    conditions = tmp_path / "conditions.json"
    save_index(path, populated())
    conditions.write_text(json.dumps(CRITERIA))
    response = subprocess.run(
        [
            sys.executable,
            "-m",
            "frameseek.occurrences",
            "search",
            "--index",
            str(path),
            "--conditions",
            str(conditions),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    result = json.loads(response.stdout)
    assert result["occurrences"][0]["end"] == 24
    assert result["model_requests_sent"] == 0
