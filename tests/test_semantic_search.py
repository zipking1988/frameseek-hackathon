"""Cross-language concept alignment is not new recognition or arbitrary NLU."""

from frameseek.semantic_search import caption_matches, interpret


def test_complete_conditions_not_single_character_overlap():
    question = "找出穿白色球衣的人，列出所有出现时间"
    terms, problems = interpret(question)
    assert not problems and set(terms) == {"white", "jersey", "person"}
    assert caption_matches("一名身穿白色球衣的球员在门口。", terms)
    assert not caption_matches("一名穿蓝色球衣的球员。", terms)
    assert not caption_matches("白色箱子旁边有球员。", terms)
    english, problems = interpret("find people wearing white jersey, list all times")
    assert not problems and set(english) == set(terms)


def test_video_location_words_are_not_treated_as_visual_attributes():
    terms, problems = interpret("找出视频中出现的人")
    assert not problems and terms == ["person"]


def test_english_in_clothing_is_supported_without_dropping_spatial_relations():
    terms, problems = interpret("find a person in a white jersey")
    assert not problems and set(terms) == {"person", "white", "jersey"}
    assert set(interpret("find a person in a red hat")[0]) == {"person", "red", "hat"}
    terms, problems = interpret("find a person in a room")
    assert not problems and {"person", "in", "room"} <= set(terms)


def test_generic_white_clothing_matches_bound_shirt_or_jersey_not_a_hat():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(3)
    subjects = [
        [{"id": "a", "facts": {"kind": ["person"], "garment": ["white jersey"]}}],
        [{"id": "b", "facts": {"kind": ["person"], "garment": ["white shirt"]}}],
        [
            {
                "id": "c",
                "facts": {"kind": ["person"], "garment": ["blue jersey"], "hat": ["white hat"]},
            },
            {"id": "d", "facts": {"kind": ["parcel"], "garment": ["white shirt"]}},
        ],
    ]
    index = checkpoint(
        index, [observation(i, subjects=s) for i, s in enumerate(subjects)], index.snapshot()
    )
    for question in ("找出穿白色衣服的人", "find people wearing white clothes"):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        assert any(c.operator == "color_item" and c.value == "white clothing" for c in conditions)
        hits = search_occurrences(index, conditions)["occurrences"]
        assert [frame["t"] for hit in hits for frame in hit["evidence"]] == [0, 1]


def test_gender_qualified_person_query_requires_observed_gender_on_same_subject():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(3)
    subjects = [
        [{"id": "a", "facts": {"kind": ["person"], "gender": ["male"]}}],
        [{"id": "b", "facts": {"kind": ["person"], "gender": ["female"]}}],
        [{"id": "c", "facts": {"kind": ["person"]}}],
    ]
    index = checkpoint(
        index,
        [observation(i, subjects=s, complete=False) for i, s in enumerate(subjects)],
        index.snapshot(),
    )
    for question in ("找出男人", "find a man"):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        result = search_occurrences(index, conditions)
        assert [hit["start"] for hit in result["occurrences"]] == [0]
        assert "vid_offline_f2" in result["coverage"]["uncertain_frame_ids"]
    for question in ("找出女人", "find a woman"):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        assert [hit["start"] for hit in search_occurrences(index, conditions)["occurrences"]] == [1]

    without_gender = catalog(1)
    without_gender = checkpoint(without_gender, [observation(0)], without_gender.snapshot())
    assert resolve_conditions(without_gender, "找出男人")[1]

    kind_label = catalog(1)
    kind_label = checkpoint(
        kind_label,
        [observation(0, subjects=[{"id": "p", "facts": {"kind": ["man"]}}])],
        kind_label.snapshot(),
    )
    conditions, problems = resolve_conditions(kind_label, "find a man")
    assert not problems and search_occurrences(kind_label, conditions)["occurrences"]


def test_compound_person_attributes_do_not_combine_separate_subjects():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(4)
    subjects = [
        [
            {
                "id": "a",
                "facts": {
                    "kind": ["person"],
                    "gender": ["male"],
                    "headwear": ["red hat"],
                    "garment": ["white shirt"],
                },
            }
        ],
        [
            {
                "id": "b",
                "facts": {
                    "kind": ["person"],
                    "gender": ["male"],
                    "headwear": ["red hat"],
                    "garment": ["blue shirt"],
                },
            }
        ],
        [
            {
                "id": "c",
                "facts": {
                    "kind": ["person"],
                    "gender": ["female"],
                    "headwear": ["red hat"],
                    "garment": ["white shirt"],
                },
            }
        ],
        [
            {
                "id": "d",
                "facts": {"kind": ["person"], "gender": ["male"], "garment": ["white shirt"]},
            },
            {"id": "e", "facts": {"kind": ["person"], "gender": ["male"], "headwear": ["red hat"]}},
        ],
    ]
    index = checkpoint(
        index, [observation(i, subjects=s) for i, s in enumerate(subjects)], index.snapshot()
    )
    for question in (
        "找出戴红帽穿白色衣服的男人",
        "find a man wearing a red hat and white clothes",
        "视频里面有没有红色帽子 白色衣服的男人 可以帮我列出来 并且知道timeline",
    ):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        hits = search_occurrences(index, conditions)["occurrences"]
        assert [hit["start"] for hit in hits] == [0]


def test_garment_trim_does_not_become_primary_garment_color():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {
                        "id": "a",
                        "facts": {
                            "kind": ["person"],
                            "gender": ["male"],
                            "garment": ["blue jersey with yellow trim"],
                            "headwear": ["yellow headband"],
                        },
                    }
                ],
            )
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "穿蓝色球衣戴黄色头带的男人")
    assert not problems
    assert search_occurrences(index, conditions)["occurrences"]
    conditions, problems = resolve_conditions(index, "穿黄色球衣戴黄色头带的男人")
    assert problems or not search_occurrences(index, conditions)["occurrences"]


def test_multi_color_accents_are_not_confused_with_primary_garment():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(2, complete=False)
    rows = [
        observation(
            0,
            [
                {
                    "id": "p0",
                    "facts": {
                        "kind": ["person"],
                        "garment": ["white jersey with blue and red accents"],
                    },
                }
            ],
        ),
        observation(
            1,
            [
                {
                    "id": "p1",
                    "facts": {
                        "kind": ["person"],
                        "garment": ["white jersey with red and black accents"],
                    },
                }
            ],
        ),
    ]
    index = checkpoint(index, rows, index.snapshot())
    conditions, problems = resolve_conditions(index, "找出白色球衣的人")
    assert not problems
    assert [hit["start"] for hit in search_occurrences(index, conditions)["occurrences"]] == [0, 1]
    for color in ("蓝色", "红色", "黑色"):
        conditions, problems = resolve_conditions(index, f"找出{color}球衣的人")
        assert problems or not search_occurrences(index, conditions)["occurrences"]


def test_garment_number_does_not_hide_its_primary_color():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1, complete=False)
    index = checkpoint(
        index,
        [
            observation(
                0,
                [
                    {
                        "id": "p",
                        "facts": {"kind": ["person"], "garment": ["blue jersey with number 0"]},
                    }
                ],
            )
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出蓝色球衣的人")
    assert not problems
    assert [hit["start"] for hit in search_occurrences(index, conditions)["occurrences"]] == [0]
    conditions, problems = resolve_conditions(index, "找出红色球衣的人")
    assert problems or not search_occurrences(index, conditions)["occurrences"]
    conditions, problems = resolve_conditions(index, "找出蓝色球衣号码 1 的人")
    assert problems or not search_occurrences(index, conditions)["occurrences"]


def test_english_existence_and_time_phrasings_keep_visual_conditions_and_negations():
    for question in (
        "Where does the box appear?",
        "When does the box appear?",
        "Is there a box in the video?",
        "Show me every appearance of the box.",
    ):
        terms, problems = interpret(question)
        assert not problems and terms == ["parcel"]
    assert set(interpret("Where is the red box?")[0]) == {"red", "parcel"}
    for question in ("Is there no box?", "Isn't there a red box?", "find a person without a hat"):
        assert interpret(question)[1]
    assert "room" in interpret("Where is the box in the room?")[0]


def test_common_timestamp_and_evidence_request_is_not_a_visual_condition():
    terms, problems = interpret("视频里箱子出现在哪些时间？请列出画面证据。")
    assert not problems and terms == ["parcel"]
    assert interpret("棕色箱子什么时候出现？")[0] == ["brown", "parcel"]
    from frameseek.semantic_search import remove_time_range

    terms, problems = interpret(
        remove_time_range("请在05:04至05:12找出穿蓝色球衣并戴黄色头带的球员，列出出现时间和画面。")
    )
    assert not problems and set(terms) == {"blue", "yellow", "jersey", "headband", "person"}
    assert "出现在哪些时间" in remove_time_range("请在00:00至00:08找人物，它出现在哪些时间？")
    assert "from Japan" in remove_time_range("find a person from Japan")
    assert "from" not in remove_time_range("find a person from 00:04-00:06")
    assert set(interpret(remove_time_range("find a box from 00:04 to 00:06"))[0]) == {"parcel"}
    assert set(interpret(remove_time_range("find a box between 00:04 and 00:06"))[0]) == {"parcel"}
    assert remove_time_range("compare 00:04 and 00:06") == "compare 00:04 and 00:06"
    quoted = 'Find text "00:04 to 00:06" from 00:10 to 00:12'
    assert remove_time_range(quoted).strip() == 'Find text "00:04 to 00:06"'
    assert remove_time_range('Find text "00:04 to 00:06"') == 'Find text "00:04 to 00:06"'
    assert set(interpret("视频里箱子出现在哪些时间？只找红色箱子。")[0]) == {
        "parcel",
        "red",
        "只找",
    }


def test_brown_box_conditions_match_only_the_same_subject():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "box", "facts": {"kind": ["box"], "color": ["brown"]}},
                    {"id": "person", "facts": {"kind": ["person"], "color": ["red"]}},
                ],
            )
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出棕色箱子，列出出现时间。")
    assert not problems
    result = search_occurrences(index, [condition.model_dump() for condition in conditions])
    assert result["occurrences"][0]["evidence"][0]["subjects"] == ["box"]
    conditions, problems = resolve_conditions(index, "找出红色箱子，列出出现时间。")
    assert not problems
    assert not search_occurrences(index, [condition.model_dump() for condition in conditions])[
        "occurrences"
    ]


def test_known_but_unobserved_kind_and_color_keep_coverage_uncertain():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[{"id": "box", "facts": {"kind": ["box"], "color": ["brown"]}}],
                complete=False,
            )
        ],
        index.snapshot(),
    )
    for question in ("有没有人物？", "找出红色箱子"):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        result = search_occurrences(index, [condition.model_dump() for condition in conditions])
        assert not result["occurrences"]
        assert not result["coverage"]["exhaustive_under_index"]
    assert resolve_conditions(index, "找出紫色箱子")[1]  # Unknown language is not dropped.
    unrelated = catalog(1)
    unrelated = checkpoint(
        unrelated,
        [observation(0, subjects=[{"id": "item", "facts": {"label": ["barcode"]}}])],
        unrelated.snapshot(),
    )
    assert resolve_conditions(unrelated, "有没有人物？")[1]  # No grounded kind field.


def test_holding_relationship_binds_person_and_object_without_cross_subject_inference():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(2)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {
                        "id": "player",
                        "facts": {"kind": ["person"], "action": ["holding basketball"]},
                    }
                ],
            ),
            observation(
                1,
                subjects=[
                    {"id": "player", "facts": {"kind": ["person"]}},
                    {"id": "other", "facts": {"kind": ["basketball"]}},
                ],
            ),
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出拿着篮球的人，列出出现时间")
    assert not problems
    results = search_occurrences(index, [condition.model_dump() for condition in conditions])
    assert [hit["start"] for hit in results["occurrences"]] == [0]
    _, problems = resolve_conditions(index, "找出拿着箱子的人，列出出现时间")
    assert problems
    _, problems = resolve_conditions(index, "找出拿着红色篮球的人，列出出现时间")
    assert problems  # A person's clothing color does not establish the ball's color.


def test_garment_color_consumption_cannot_drop_the_color_of_a_held_object():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {
                        "id": "person",
                        "facts": {
                            "kind": ["person"],
                            "headwear": ["red hat"],
                            "action": ["holding basketball"],
                        },
                    },
                    {"id": "hat-only", "facts": {"kind": ["person"], "headwear": ["red hat"]}},
                    {
                        "id": "ball-only",
                        "facts": {"kind": ["person"], "action": ["holding basketball"]},
                    },
                ],
            )
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出戴红色帽子拿着红色篮球的人")
    assert problems  # Red headwear cannot prove that the held basketball is red.
    assert not conditions or any("object" in problem for problem in problems)
    _, problems = resolve_conditions(
        index, "find a person wearing a red hat holding a red basketball"
    )
    assert problems

    # A color explicitly bound only to the hat need not block a separately
    # evidenced, uncolored holding relation on the very same subject.
    from frameseek.occurrences import search_occurrences

    for question in (
        "找出戴红色帽子拿着篮球的人",
        "find a person wearing a red hat holding a basketball",
    ):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        found = search_occurrences(index, [condition.model_dump() for condition in conditions])
        assert found["occurrences"][0]["evidence"][0]["subjects"] == ["person"]


def test_quoted_visible_text_matches_observed_text_not_caption():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(2)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "t", "facts": {"kind": ["text"], "text": ["NBA FINALS - TOR LEADS 3-1"]}}
                ],
            ),
            observation(
                1,
                subjects=[
                    {"id": "p", "facts": {"kind": ["person"], "description": ["NBA FINALS"]}}
                ],
            ),
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出文字“NBA FINALS”出现时间")
    assert not problems
    result = search_occurrences(index, [condition.model_dump() for condition in conditions])
    assert [hit["start"] for hit in result["occurrences"]] == [0]
    conditions, problems = resolve_conditions(index, "找出文字“missing”出现时间")
    assert not problems
    assert not search_occurrences(index, [condition.model_dump() for condition in conditions])[
        "occurrences"
    ]
    assert resolve_conditions(index, "找出红色文字“NBA FINALS”出现时间")[1]
    assert resolve_conditions(index, "找出文字“  ”出现时间")[1]
    from frameseek.occurrences import Condition, Subject, subject_verdict

    parcel_text = Subject(id="t", facts={"text": ["parcel"]})
    assert (
        subject_verdict(parcel_text, [Condition(field="text", value="box", operator="contains")])
        == "no"
    )


def test_compound_clothing_value_is_matched_as_one_observed_attribute():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    row = observation(
        0,
        subjects=[
            {"id": "p", "facts": {"kind": ["player"], "garment": ["blue jersey", "red hat"]}}
        ],
    )
    index = checkpoint(index, [row], index.snapshot())
    conditions, problems = resolve_conditions(index, "找出穿蓝色球衣的人")
    assert not problems
    assert search_occurrences(index, [c.model_dump() for c in conditions])["occurrences"]
    conditions, problems = resolve_conditions(index, "找出穿红色球衣的人")
    assert problems  # Red hat + blue jersey cannot become a red jersey.


def test_multicolor_garment_stays_bound_to_one_item_and_subject():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {
                        "id": "a",
                        "facts": {
                            "kind": ["person"],
                            "garment": ["blue and yellow jersey"],
                            "headwear": ["yellow headband"],
                        },
                    },
                    {
                        "id": "b",
                        "facts": {
                            "kind": ["person"],
                            "garment": ["blue and yellow jersey"],
                            "headwear": ["red hat"],
                        },
                    },
                ],
            )
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出穿蓝色球衣并戴黄色头带的人")
    assert not problems
    hits = search_occurrences(index, [condition.model_dump() for condition in conditions])
    assert hits["occurrences"][0]["evidence"][0]["subjects"] == ["a"]
    ambiguous = catalog(1)
    ambiguous = checkpoint(
        ambiguous,
        [
            observation(
                0,
                subjects=[
                    {
                        "id": "p",
                        "facts": {"kind": ["person"], "garment": ["blue jersey and yellow hat"]},
                    }
                ],
            )
        ],
        ambiguous.snapshot(),
    )
    assert resolve_conditions(ambiguous, "找出黄色球衣的人")[1]


def test_multiple_equivalent_garment_values_are_all_searched_without_cross_binding():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import Condition, search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(3)
    index = checkpoint(
        index,
        [
            observation(
                0, subjects=[{"id": "a", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}]
            ),
            observation(
                1,
                subjects=[
                    {
                        "id": "b",
                        "facts": {"kind": ["person"], "garment": ["blue and yellow jersey"]},
                    }
                ],
            ),
            observation(
                2,
                subjects=[
                    {
                        "id": "c",
                        "facts": {"kind": ["person"], "garment": ["blue jersey and yellow hat"]},
                    }
                ],
            ),
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出蓝色球衣的人")
    assert not problems
    assert any(c.operator == "color_item" for c in conditions)
    result = search_occurrences(index, [c.model_dump() for c in conditions])
    hits = result["occurrences"]
    assert [frame["t"] for hit in hits for frame in hit["evidence"]] == [0, 1]
    assert not result["coverage"]["exhaustive_under_index"]
    # An explicit exact filter must not silently widen to the multicolor value.
    exact = search_occurrences(index, [Condition(field="garment", value="blue jersey")])
    assert [hit["start"] for hit in exact["occurrences"]] == [0]
    import pytest

    with pytest.raises(ValueError):
        Condition(field="text", value="blue jersey", operator="color_item")
    with pytest.raises(ValueError):
        Condition(field="garment", value="blue jersey hat", operator="color_item")


def test_multicolor_query_requires_both_colors_on_one_garment():
    import pytest
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import Condition, search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(4)
    subjects = [
        [{"id": "a", "facts": {"kind": ["person"], "garment": ["blue and yellow jersey"]}}],
        [{"id": "b", "facts": {"kind": ["person"], "garment": ["blue jersey", "yellow jersey"]}}],
        [
            {"id": "c", "facts": {"kind": ["person"], "garment": ["blue jersey"]}},
            {"id": "d", "facts": {"kind": ["person"], "garment": ["yellow jersey"]}},
        ],
        [{"id": "e", "facts": {"kind": ["person"], "garment": ["blue jersey"]}}],
    ]
    index = checkpoint(
        index, [observation(i, subjects=s) for i, s in enumerate(subjects)], index.snapshot()
    )
    for question in ("找出穿蓝色和黄色球衣的人", "find people wearing blue and yellow jersey"):
        conditions, problems = resolve_conditions(index, question)
        assert not problems
        assert any(
            c.operator == "color_item" and c.value == "blue yellow jersey" for c in conditions
        )
        result = search_occurrences(index, conditions)
        assert [frame["t"] for hit in result["occurrences"] for frame in hit["evidence"]] == [0]
        assert not result["coverage"]["exhaustive_under_index"]
    with pytest.raises(ValueError):
        Condition(field="garment", value="blue blue jersey", operator="color_item")


def test_separate_unbound_colors_do_not_become_colored_clothing():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    row = observation(
        0,
        subjects=[
            {
                "id": "p",
                "facts": {
                    "kind": ["person"],
                    "garment": ["jersey", "hat"],
                    "color": ["blue", "yellow"],
                },
            }
        ],
    )
    index = checkpoint(index, [row], index.snapshot())
    _, problems = resolve_conditions(index, "找出穿蓝色球衣戴黄色帽子的人")
    assert problems


def test_multiple_colored_items_must_belong_to_same_subject():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    row = observation(
        0,
        subjects=[
            {
                "id": "a",
                "facts": {
                    "kind": ["player"],
                    "garment": ["blue jersey"],
                    "headwear": ["yellow headband"],
                },
            },
            {
                "id": "b",
                "facts": {
                    "kind": ["player"],
                    "garment": ["yellow jersey"],
                    "headwear": ["blue headband"],
                },
            },
        ],
    )
    index = checkpoint(index, [row], index.snapshot())
    conditions, problems = resolve_conditions(index, "找出穿蓝色球衣并戴黄色头带的人")
    assert not problems
    hits = search_occurrences(index, [c.model_dump() for c in conditions])["occurrences"]
    assert hits[0]["evidence"][0]["subjects"] == ["a"]


def test_named_color_binds_to_the_single_item_in_its_own_field():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    row = observation(
        0,
        subjects=[
            {
                "id": "p",
                "facts": {
                    "kind": ["person"],
                    "garment": ["blue jersey"],
                    "headwear": ["headband"],
                    "headwear_color": ["yellow"],
                },
            }
        ],
    )
    index = checkpoint(index, [row], index.snapshot())
    conditions, problems = resolve_conditions(index, "找出穿蓝色球衣并戴黄色头带的人")
    assert not problems
    assert search_occurrences(index, [condition.model_dump() for condition in conditions])[
        "occurrences"
    ]


def test_named_color_cannot_bind_to_multiple_items_on_another_subject():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    row = observation(
        0,
        subjects=[
            {
                "id": "a",
                "facts": {
                    "kind": ["person"],
                    "headwear": ["headband"],
                    "headwear_color": ["yellow"],
                },
            },
            {
                "id": "b",
                "facts": {
                    "kind": ["person"],
                    "headwear": ["headband", "hat"],
                    "headwear_color": ["yellow"],
                },
            },
        ],
    )
    index = checkpoint(index, [row], index.snapshot())
    conditions, problems = resolve_conditions(index, "找出戴黄色头带的人")
    assert not problems
    result = search_occurrences(index, conditions)
    assert result["occurrences"][0]["evidence"][0]["subjects"] == ["a"]
    assert not result["coverage"]["exhaustive_under_index"]


def test_compound_and_named_color_bindings_both_match_without_cross_subjects():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(5)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[
                    {"id": "a", "facts": {"kind": ["person"], "headwear": ["yellow headband"]}}
                ],
            ),
            observation(
                1,
                subjects=[
                    {
                        "id": "b",
                        "facts": {
                            "kind": ["person"],
                            "headwear": ["headband"],
                            "headwear_color": ["yellow"],
                        },
                    }
                ],
            ),
            observation(
                2,
                subjects=[
                    {
                        "id": "c",
                        "facts": {
                            "kind": ["person"],
                            "headwear": ["headband"],
                            "headwear_color": ["red"],
                        },
                    }
                ],
            ),
            observation(
                3,
                subjects=[
                    {
                        "id": "d",
                        "facts": {
                            "kind": ["person"],
                            "headwear": ["headband", "hat"],
                            "headwear_color": ["yellow"],
                        },
                    }
                ],
            ),
            observation(
                4,
                subjects=[
                    {"id": "e", "facts": {"kind": ["person"], "headwear": ["headband"]}},
                    {"id": "f", "facts": {"kind": ["person"], "headwear_color": ["yellow"]}},
                ],
            ),
        ],
        index.snapshot(),
    )
    conditions, problems = resolve_conditions(index, "找出黄色头带的人")
    assert not problems and any(c.operator == "color_item" for c in conditions)
    result = search_occurrences(index, [c.model_dump() for c in conditions])
    assert [frame["t"] for hit in result["occurrences"] for frame in hit["evidence"]] == [0, 1]
    assert not result["coverage"]["exhaustive_under_index"]


def test_equivalent_kind_values_in_same_field_do_not_cause_ambiguity():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    row = observation(
        0,
        subjects=[
            {"id": "a", "facts": {"kind": ["player"]}},
            {"id": "b", "facts": {"kind": ["person"]}},
        ],
    )
    index = checkpoint(index, [row], index.snapshot())
    conditions, problems = resolve_conditions(index, "找出视频中出现的人")
    assert not problems
    hits = search_occurrences(index, [c.model_dump() for c in conditions])["occurrences"]
    assert hits[0]["evidence"][0]["subjects"] == ["a", "b"]


def test_unknown_clauses_and_negation_are_not_silently_discarded():
    terms, problems = interpret("white jersey jumping")
    assert "jumping" in terms and not problems
    assert not caption_matches("穿白色球衣的球员", terms)
    assert interpret("white or blue")[1]
    assert interpret("没有红色帽子的人")[1]
    assert set(interpret("视频里面有没有红色帽子？")[0]) == {"red", "hat"}
    assert interpret("有没有不戴红色帽子的人？")[1]


def test_common_question_forms_retrieve_only_their_observed_conditions():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [
            observation(
                0,
                subjects=[{"id": "p", "facts": {"kind": ["person"], "garment": ["white jersey"]}}],
            )
        ],
        index.snapshot(),
    )
    for question in (
        "找一个穿白色球衣的球员",
        "穿白色球衣的人有哪些？",
        "哪里有穿白色球衣的人？",
        "画面里有穿白色球衣的人吗？",
        "when do people wearing white jerseys appear",
    ):
        conditions, problems = resolve_conditions(index, question)
        assert not problems, (question, problems)
        assert search_occurrences(index, [condition.model_dump() for condition in conditions])[
            "occurrences"
        ]
    terms, problems = interpret("有白色球衣吗？")
    assert set(terms) == {"white", "jersey"} and not problems
    assert interpret("画面里有没有不穿白色球衣的人吗？")[1]
    assert "跳舞" in interpret("画面里有穿白色球衣跳舞的人吗？")[0]


def test_legacy_bilingual_candidates_stay_unverified_and_require_all_conditions(tmp_path):
    import json

    from test_visual_search import source

    from frameseek.index_service import chat_answer, search_source

    video = "vid_fixture"
    directory = tmp_path / video / "frames"
    directory.mkdir(parents=True)
    for i in range(3):
        (directory / f"f{i}.jpg").write_bytes(b"fixture-only")
    manifest = source(["白色球衣的球员。", "蓝色球衣的球员。", "球员在红色箱子旁。"])
    row = {
        "id": video,
        "generation": 2,
        "metadata": json.dumps({"duration": 40.0}),
        "manifest": json.dumps(manifest),
    }
    for question in ("找出穿白色球衣的人，列出所有出现时间", "white jersey person"):
        result = search_source(tmp_path, row, question)
        assert result["total_candidates"] == 1
        assert result["frames"][0]["frame_id"] == "vid_fixture_f0"
        assert result["verified_matches"] is False
        assert not chat_answer(result)["events"]
        assert "lacks a complete object observation index" in chat_answer(result)["answer"]


def test_aggregate_questions_do_not_become_false_zero_occurrence_answers(tmp_path):
    from frameseek.index_service import search_source

    source = {"id": "vid_unobserved", "generation": 1, "metadata": '{"duration":12}'}
    result = search_source(tmp_path, source, "谁投的3分最多？")
    assert result["kind"] == "clarification_required"
    assert result["model_requests_sent"] == 0
    assert "totals or rankings" in result["message"]
