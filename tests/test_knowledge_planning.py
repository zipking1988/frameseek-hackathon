"""Supplement candidates are bounded leads, not extra model dispatch or proof."""

import pytest
from test_occurrences import catalog, checkpoint, observation

from frameseek.knowledge_planning import plan_supplement


def test_selects_at_most_two_related_windows_with_original_frame_ids():
    index = catalog(24)
    for start in range(0, 24, 8):
        rows = [
            observation(
                i,
                subjects=[{"id": "a", "facts": {"kind": ["person"], "color": ["white"]}}],
                complete=False,
            )
            for i in range(start, start + 8)
        ]
        index = checkpoint(index, rows, index.snapshot())
    result = plan_supplement(index, "find people wearing red hat", "a" * 64)
    assert result["kind"] == "supplement_candidates"
    assert result["missing_terms"] == ["red", "hat"]
    assert [(w["start"], w["end"]) for w in result["windows"]] == [(0, 8), (8, 16)]
    assert all(len(w["frame_ids"]) <= 8 for w in result["windows"])
    assert result["candidates_are_evidence"] is False
    assert result["model_requests_sent"] == 0


def test_no_grounded_subject_does_not_authorize_full_scan():
    index = checkpoint(catalog(1), [observation(0)], catalog(1).snapshot())
    result = plan_supplement(index, "find bicycle", "b" * 64)
    assert result["kind"] == "coverage_gap"
    assert result["windows"] == []


def test_equivalent_subject_kind_labels_ground_supplement_candidates():
    index = catalog(2)
    rows = [
        observation(0, subjects=[{"id": "a", "facts": {"kind": ["person"]}}]),
        observation(1, subjects=[{"id": "b", "facts": {"kind": ["player"]}}]),
    ]
    index = checkpoint(index, rows, index.snapshot())
    result = plan_supplement(index, "找出戴黄色头带的人", "b" * 64)
    assert result["kind"] == "supplement_candidates"
    assert "person" not in result["missing_terms"]
    assert result["windows"][0]["frame_ids"] == ["vid_offline_f0", "vid_offline_f1"]


def test_caption_prioritizes_grounded_candidate_but_does_not_create_evidence():
    index = catalog(24)
    data = index.model_dump(mode="json")
    data["frames"][17]["caption"] = "A person with a yellow headband."
    index = type(index).model_validate(data)
    for start in range(0, 24, 8):
        rows = [
            observation(i, subjects=[{"id": "p", "facts": {"kind": ["person"]}}])
            for i in range(start, start + 8)
        ]
        index = checkpoint(index, rows, index.snapshot())
    result = plan_supplement(index, "找出戴黄色头带的人", "b" * 64, max_windows=1)
    assert result["kind"] == "supplement_candidates"
    assert result["windows"][0]["start"] == 16
    assert result["candidates_are_evidence"] is False and result["model_requests_sent"] == 0

    # Description alone cannot ground a missing subject or permit a full scan.
    without_subject = catalog(24)
    data = without_subject.model_dump(mode="json")
    data["frames"][17]["caption"] = "A person with a yellow headband."
    without_subject = type(without_subject).model_validate(data)
    for start in range(0, 24, 8):
        without_subject = checkpoint(
            without_subject,
            [observation(i, subjects=[]) for i in range(start, start + 8)],
            without_subject.snapshot(),
        )
    assert (
        plan_supplement(without_subject, "找出戴黄色头带的人", "b" * 64)["kind"] == "coverage_gap"
    )


def test_explicit_time_scope_never_targets_outside_requested_range():
    index = catalog(24)
    for start in range(0, 24, 8):
        rows = [
            observation(i, subjects=[{"id": "p", "facts": {"kind": ["person"]}}])
            for i in range(start, start + 8)
        ]
        index = checkpoint(index, rows, index.snapshot())
    plan = plan_supplement(index, "请在00:10至00:18找出戴红帽的人", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert [(window["start"], window["end"]) for window in plan["windows"]] == [(10, 16), (16, 18)]
    assert all(
        10 <= int(frame_id.rsplit("f", 1)[1]) < 18
        for window in plan["windows"]
        for frame_id in window["frame_ids"]
    )
    invalid = plan_supplement(index, "00:20至00:40找人", "a" * 64)
    assert invalid["kind"] == "clarification_required" and not invalid["windows"]


def test_short_explicit_scope_can_locate_unobserved_visible_text_without_full_scan():
    index = catalog(24)
    for start in range(0, 24, 8):
        index = checkpoint(
            index,
            [
                observation(i, subjects=[{"id": "p", "facts": {"kind": ["person"]}}])
                for i in range(start, start + 8)
            ],
            index.snapshot(),
        )
    plan = plan_supplement(index, "请在00:10至00:18找出文字“EXIT”", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert [(w["start"], w["end"]) for w in plan["windows"]] == [(10, 16), (16, 18)]
    assert [f for w in plan["windows"] for f in w["frame_ids"]] == [
        f"vid_offline_f{i}" for i in range(10, 18)
    ]
    assert plan["candidates_are_evidence"] is False and plan["model_requests_sent"] == 0
    assert plan_supplement(index, "找出文字“EXIT”", "a" * 64)["kind"] == "coverage_gap"
    assert (
        plan_supplement(index, "请在00:01至00:18找出文字“EXIT”", "a" * 64)["kind"] == "coverage_gap"
    )


def test_supplement_only_targets_unconfirmed_frames_when_some_matches_exist():
    index = catalog(8)
    rows = []
    for i in range(8):
        facts = {"kind": ["person"]}
        if i < 5:
            facts["garment"] = ["blue jersey"]
        rows.append(observation(i, subjects=[{"id": "p", "facts": facts}], complete=False))
    index = checkpoint(index, rows, index.snapshot())
    result = plan_supplement(index, "找出穿蓝色球衣的人", "a" * 64)
    assert result["kind"] == "supplement_candidates"
    assert result["windows"][0]["frame_ids"] == [
        "vid_offline_f5",
        "vid_offline_f6",
        "vid_offline_f7",
    ]


def test_short_scope_can_check_unobserved_frame_even_after_an_exact_match():
    index = catalog(3)
    index = checkpoint(
        index,
        [
            observation(
                0, subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}]
            ),
            observation(1, subjects=[], complete=False),
            observation(2, subjects=[], complete=True),
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "请在00:00至00:03找出穿蓝色球衣的人", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert plan["windows"][0]["frame_ids"] == ["vid_offline_f1"]


def test_short_scope_does_not_recheck_complete_negative_frames_without_a_match():
    index = catalog(3)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["red jersey"]}}],
                complete=True,
            ),
            observation(1, subjects=[], complete=True),
            observation(
                2,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}],
                complete=True,
            ),
        ],
        index.snapshot(),
    )
    # A blue value exists elsewhere in the index, so the query's conditions can
    # be resolved even though the requested interval only contains negatives.
    plan = plan_supplement(index, "请在00:00至00:02找出穿蓝色球衣的人", "a" * 64)
    assert plan["kind"] == "coverage_gap"
    assert plan["windows"] == []


def test_short_scope_still_checks_incomplete_negative_frames():
    index = catalog(3)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["red jersey"]}}],
                complete=True,
            ),
            observation(
                1,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["red jersey"]}}],
                complete=False,
            ),
            observation(
                2,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}],
                complete=True,
            ),
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "请在00:00至00:02找出穿蓝色球衣的人", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert plan["windows"][0]["frame_ids"] == ["vid_offline_f1"]


def test_short_scope_checks_empty_incomplete_frames_without_an_exact_match():
    index = catalog(3)
    index = checkpoint(
        index,
        [
            observation(0, subjects=[{"id": "p", "facts": {"kind": ["person"]}}], complete=False),
            observation(1, subjects=[], complete=False),
            observation(
                2,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}],
                complete=True,
            ),
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "请在00:00至00:02找出穿蓝色球衣的人", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert plan["windows"][0]["frame_ids"] == ["vid_offline_f0", "vid_offline_f1"]


def test_short_scope_includes_empty_incomplete_frame_alongside_other_candidates():
    index = catalog(3)
    index = checkpoint(
        index,
        [
            observation(
                0, subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}]
            ),
            observation(1, subjects=[{"id": "p", "facts": {"kind": ["person"]}}], complete=False),
            observation(2, subjects=[], complete=False),
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "请在00:00至00:03找出穿蓝色球衣的人", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert plan["windows"][0]["frame_ids"] == ["vid_offline_f1", "vid_offline_f2"]


def test_matching_subject_does_not_hide_other_uncertain_subject_in_same_frame():
    index = catalog(2)
    rows = [
        observation(
            0,
            subjects=[
                {"id": "confirmed", "facts": {"kind": ["person"], "garment": ["blue jersey"]}},
                {"id": "uncertain", "facts": {"kind": ["person"]}},
            ],
            complete=False,
        ),
        observation(
            1,
            subjects=[
                {"id": "confirmed", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}
            ],
            complete=False,
        ),
    ]
    index = checkpoint(index, rows, index.snapshot())
    result = plan_supplement(index, "找出穿蓝色球衣的人", "a" * 64)
    assert result["kind"] == "supplement_candidates"
    assert result["windows"][0]["frame_ids"] == ["vid_offline_f0"]


def test_short_scope_checks_unknown_subject_even_without_grounded_attributes():
    index = catalog(2)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "confirmed", "facts": {"kind": ["person"], "garment": ["blue jersey"]}},
                    {"id": "unknown", "facts": {"pose": ["standing"]}},
                ],
                complete=False,
            )
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "请在00:00至00:01找出穿蓝色球衣的人", "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert plan["windows"][0]["frame_ids"] == ["vid_offline_f0"]
    assert plan["model_requests_sent"] == 0


def test_short_scope_single_condition_reuses_already_matched_frame():
    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "match", "facts": {"garment": ["jersey"]}},
                    {"id": "unknown", "facts": {"pose": ["standing"]}},
                ],
                complete=False,
            )
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "请在00:00至00:01找出球衣", "a" * 64)
    assert plan["kind"] == "coverage_gap"
    assert plan["windows"] == []
    assert plan["model_requests_sent"] == 0


def test_unscoped_query_does_not_infer_an_unknown_subject_from_a_match():
    index = catalog(2)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "match", "facts": {"garment": ["jersey"]}},
                    {"id": "unknown", "facts": {"pose": ["standing"]}},
                ],
                complete=False,
            ),
            observation(1, subjects=[{"id": "unknown", "facts": {"pose": ["standing"]}}]),
        ],
        index.snapshot(),
    )
    plan = plan_supplement(index, "找出球衣", "a" * 64)
    assert plan["kind"] == "coverage_gap"
    assert plan["windows"] == []
    assert plan["model_requests_sent"] == 0


def test_bound_match_does_not_hide_separate_ambiguous_color_on_another_subject():
    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "bound", "facts": {"kind": ["person"], "garment": ["white jersey"]}},
                    {
                        "id": "unbound",
                        "facts": {"kind": ["person"], "garment": ["jersey"], "color": ["white"]},
                    },
                ],
                complete=False,
            )
        ],
        index.snapshot(),
    )
    question = "找出穿白色球衣的人"
    conditions, problems = resolve_conditions(index, question)
    assert not problems
    result = search_occurrences(index, [item.model_dump() for item in conditions])
    assert result["occurrences"][0]["evidence"][0]["subjects"] == ["bound"]
    plan = plan_supplement(index, question, "a" * 64)
    assert plan["kind"] == "supplement_candidates"
    assert plan["windows"][0]["frame_ids"] == ["vid_offline_f0"]


def test_unsupported_aggregation_and_audio_do_not_create_visual_candidates():
    index = checkpoint(catalog(1), [observation(0)], catalog(1).snapshot())
    for question in ("how many people", "说了什么"):
        result = plan_supplement(index, question, "c" * 64)
        assert result["kind"] == "capability_missing" and not result["windows"]
    with pytest.raises(ValueError, match="window_limit"):
        plan_supplement(index, "find people", "c" * 64, max_windows=3)
