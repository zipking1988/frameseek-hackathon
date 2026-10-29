"""Actual bounded supplement execution with synthetic responses only."""

import json

import pytest
from test_knowledge_compiler import decode, infer, setup_store

from frameseek.knowledge_compiler import compile_pending
from frameseek.knowledge_planning import plan_supplement
from frameseek.knowledge_supplement import (
    compile_local_supplement,
    supplement_request_id,
    visual_prompt_question,
)
from frameseek.occurrences import fingerprint, search_occurrences


class FixtureSupplement:
    fixture_only = True
    revision = "supplement-fixture-v1"
    extraction_config = "fixture-8"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def infer(self, prompt, paths, schema):
        self.calls += 1
        if self.fail:
            raise RuntimeError("unknown response")
        if "visible" in schema.get("properties", {}):
            return {"text": json.dumps({"visible": "yes"})}
        rows = []
        for i, _ in enumerate(paths):
            rows.append(
                {
                    "index": str(i),
                    "description": "synthetic matching person",
                    "subjects": [
                        {
                            "id": "person",
                            "facts": [
                                {"field": "kind", "value": "person"},
                                {"field": "color", "value": "red"},
                                {"field": "garment", "value": "hat"},
                            ],
                        }
                    ],
                }
            )
        return {"text": json.dumps({"frames": rows})}


def test_visual_question_removes_time_and_output_instructions_but_keeps_attributes():
    question = "请在05:04至05:12找出穿蓝色球衣并戴黄色头带的球员，列出出现时间和画面。"
    assert visual_prompt_question(question) == "找出穿蓝色球衣并戴黄色头带的球员"
    assert visual_prompt_question("find a person with a red hat") == "find a person with a red hat"
    assert (
        visual_prompt_question("请在05:04至05:06找出文字“NBA FINALS”出现时间")
        == "找出文字“NBA FINALS”"
    )
    assert visual_prompt_question("找出文字“出现时间”") == "找出文字“出现时间”"


def ready(tmp_path):
    store = setup_store(tmp_path)
    version = compile_pending(store, {"duration": 16}, infer, lambda: True, decode=decode)
    question = "find people wearing red hat"
    plan = plan_supplement(store.read_version(version).publishable_index(), question, version)
    return store, version, question, plan


def test_bounded_supplement_reused_without_extra_call_and_old_version_survives(tmp_path):
    store, original, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    version = compile_local_supplement(store, question, plan, processor, lambda: True)
    assert version != original
    assert processor.calls == 2 * sum(len(w["frame_ids"]) for w in plan["windows"]) == 8
    criteria = [
        {"field": "kind", "value": "person"},
        {"field": "color", "value": "red"},
        {"field": "garment", "value": "hat"},
    ]
    assert not search_occurrences(store.read_version(original).publishable_index(), criteria)[
        "occurrences"
    ]
    assert search_occurrences(store.read_version(version).publishable_index(), criteria)[
        "occurrences"
    ]
    assert compile_local_supplement(store, question, plan, processor, lambda: True) == version
    assert processor.calls == 8


def test_scoped_gap_supplement_keeps_existing_hit_and_reuses_result(tmp_path):
    from frameseek.semantic_search import remove_time_range, resolve_conditions

    store = setup_store(tmp_path)

    def initial(prompt, paths, schema):
        rows = []
        for i, _ in enumerate(paths):
            subjects = []
            if i == 0:
                subjects = [
                    {
                        "id": "p",
                        "facts": [
                            {"field": "kind", "value": "person"},
                            {"field": "garment", "value": "blue jersey"},
                        ],
                    }
                ]
            rows.append({"index": str(i), "description": "synthetic", "subjects": subjects})
        return {"text": json.dumps({"frames": rows})}

    original = compile_pending(store, {"duration": 16}, initial, lambda: True, decode=decode)
    question = "请在00:00至00:02找出穿蓝色球衣的人"
    old_index = store.read_version(original).publishable_index()
    plan = plan_supplement(old_index, question, original)
    assert plan["kind"] == "supplement_candidates"
    assert len(plan["windows"][0]["frame_ids"]) == 1
    assert plan["windows"][0]["frame_ids"][0] == old_index.frames[1].frame_id

    class BlueFixture(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            if "visible" in schema.get("properties", {}):
                self.calls += 1
                return {"text": json.dumps({"visible": "yes"})}
            self.calls += 1
            return initial(prompt, paths, schema)

    processor = BlueFixture()
    updated = compile_local_supplement(store, question, plan, processor, lambda: True)
    index = store.read_version(updated).publishable_index()
    conditions, problems = resolve_conditions(index, remove_time_range(question))
    assert not problems
    hits = search_occurrences(index, conditions, time_window={"start": 0, "end": 2})
    assert [f["t"] for h in hits["occurrences"] for f in h["evidence"]] == [0, 1]
    assert processor.calls == 2
    assert compile_local_supplement(store, question, plan, processor, lambda: True) == updated
    assert processor.calls == 2


def test_scoped_missing_text_can_be_supplemented_and_reused(tmp_path):
    from frameseek.semantic_search import remove_time_range, resolve_conditions

    store, original, _, _ = ready(tmp_path)
    question = "请在00:00至00:08找出文字“EXIT”"
    index = store.read_version(original).publishable_index()
    plan = plan_supplement(index, question, original)
    assert plan["kind"] == "supplement_candidates" and len(plan["windows"]) == 1

    class TextFixture(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            self.calls += 1
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "yes"})}
            return {
                "text": json.dumps(
                    {
                        "frames": [
                            {
                                "index": "0",
                                "description": "EXIT",
                                "subjects": [
                                    {"id": "sign", "facts": [{"field": "text", "value": "EXIT"}]}
                                ],
                            }
                        ]
                    }
                )
            }

    processor = TextFixture()
    version = compile_local_supplement(store, question, plan, processor, lambda: True)
    updated = store.read_version(version).publishable_index()
    conditions, errors = resolve_conditions(updated, remove_time_range(question))
    assert not errors
    result = search_occurrences(updated, [c.model_dump() for c in conditions])
    assert [hit["start"] for hit in result["occurrences"]] == [0, 1]
    assert not search_occurrences(index, [c.model_dump() for c in conditions])["occurrences"]
    calls = processor.calls
    assert compile_local_supplement(store, question, plan, processor, lambda: True) == version
    assert processor.calls == calls == 4


def test_duplicate_model_subject_labels_remain_separate(tmp_path):
    store, _, question, plan = ready(tmp_path)

    class Duplicated(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "yes"})}
            return {
                "text": json.dumps(
                    {
                        "frames": [
                            {
                                "index": "0",
                                "description": "fixture",
                                "subjects": [
                                    {"id": "same", "facts": [{"field": "kind", "value": "person"}]},
                                    {"id": "same", "facts": [{"field": "color", "value": "red"}]},
                                ],
                            }
                        ]
                    }
                )
            }

    version = compile_local_supplement(store, question, plan, Duplicated(), lambda: True)
    observations = store.read_version(version).supplements[-1].observations
    assert all(len({s.id for s in row.subjects}) == 2 for row in observations)
    index = store.read_version(version).publishable_index()
    criteria = [{"field": "kind", "value": "person"}, {"field": "color", "value": "red"}]
    assert not search_occurrences(index, criteria)["occurrences"]


def test_failed_unknown_supplement_is_not_resent(tmp_path):
    store, _, question, plan = ready(tmp_path)
    processor = FixtureSupplement(fail=True)
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    assert processor.calls == 1


def test_prompt_upgrade_cannot_bypass_unknown_old_dispatch(tmp_path):
    store, version, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    state = store.read_version(version)
    old = supplement_request_id(state, question, processor, "single_frame_verified_v5")
    marker = store.base / f"supplement-{old}-old-plan-0000-00-extract.dispatched.json"
    marker.write_text("{}")
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    assert processor.calls == 0


def test_local_kind_prompt_has_new_identity_and_does_not_repeat_old_receipts(tmp_path):
    from frameseek.knowledge_supplement import LOCAL_KIND_PROMPT, PROMPT

    store, version, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    processor.fixture_only = False
    processor.local_only = True
    state = store.read_version(version)
    current = supplement_request_id(state, question, processor)
    old = supplement_request_id(state, question, processor, "single_frame_verified_v6")
    assert current != old and LOCAL_KIND_PROMPT != PROMPT
    assert "first fact MUST identify" in LOCAL_KIND_PROMPT
    marker = store.base / f"supplement-{old}-old-plan-0000-00-extract.dispatched.json"
    marker.write_text("{}")
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    raw = marker.with_name(marker.name.replace(".dispatched.json", ".raw.json"))
    raw.write_text(json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"})))
    with pytest.raises(ValueError, match="knowledge_supplement_receipts_plan_conflict"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    assert processor.calls == 0


def test_local_v6_resume_reuses_matching_plan_receipt_and_old_prompt(tmp_path):
    from frameseek.knowledge_supplement import LOCAL_KIND_PROMPT, PROMPT

    store, version, question, plan = ready(tmp_path)

    class LocalProcessor(FixtureSupplement):
        fixture_only = False
        local_only = True

        def __init__(self):
            super().__init__()
            self.prompts = []

        def infer(self, prompt, paths, schema):
            self.prompts.append(prompt)
            return super().infer(prompt, paths, schema)

    processor = LocalProcessor()
    old = supplement_request_id(
        store.read_version(version), question, processor, "single_frame_verified_v6"
    )
    prefix = f"supplement-{old}-{plan['plan_id']}-{plan['windows'][0]['ordinal']:04d}-00-extract"
    (store.base / f"{prefix}.dispatched.json").write_text("{}")
    (store.base / f"{prefix}.raw.json").write_text(
        json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"}))
    )
    result = compile_local_supplement(store, question, plan, processor, lambda: True)
    assert result != version
    assert processor.calls == 2 * sum(len(w["frame_ids"]) for w in plan["windows"]) - 1
    assert any(prompt.startswith(PROMPT) for prompt in processor.prompts)
    assert not any(prompt.startswith(LOCAL_KIND_PROMPT) for prompt in processor.prompts)
    assert store.read_version(result).supplements[-1].request_fingerprint == old


def test_local_v7_receipt_resumes_with_original_prompt_not_v8(tmp_path):
    from frameseek.knowledge_supplement import LOCAL_GENDER_PROMPT, LOCAL_KIND_PROMPT

    store, version, question, plan = ready(tmp_path)

    class LocalProcessor(FixtureSupplement):
        fixture_only = False
        local_only = True

        def __init__(self):
            super().__init__()
            self.prompts = []

        def infer(self, prompt, paths, schema):
            self.prompts.append(prompt)
            return super().infer(prompt, paths, schema)

    processor = LocalProcessor()
    old = supplement_request_id(
        store.read_version(version), question, processor, "single_frame_verified_v7_local"
    )
    assert old != supplement_request_id(store.read_version(version), question, processor)
    prefix = f"supplement-{old}-{plan['plan_id']}-{plan['windows'][0]['ordinal']:04d}-00-extract"
    (store.base / f"{prefix}.dispatched.json").write_text("{}")
    (store.base / f"{prefix}.raw.json").write_text(
        json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"}))
    )
    result = compile_local_supplement(store, question, plan, processor, lambda: True)
    assert any(p.startswith(LOCAL_KIND_PROMPT) for p in processor.prompts)
    assert not any(p.startswith(LOCAL_GENDER_PROMPT) for p in processor.prompts)
    assert store.read_version(result).supplements[-1].request_fingerprint == old


def test_local_v8_receipt_resumes_without_changing_its_prompt(tmp_path):
    from frameseek.knowledge_supplement import LOCAL_COMPACT_PROMPT, LOCAL_GENDER_PROMPT

    store, version, question, plan = ready(tmp_path)

    class LocalProcessor(FixtureSupplement):
        fixture_only = False
        local_only = True

        def __init__(self):
            super().__init__()
            self.prompts = []

        def infer(self, prompt, paths, schema):
            self.prompts.append(prompt)
            return super().infer(prompt, paths, schema)

    processor = LocalProcessor()
    old = supplement_request_id(
        store.read_version(version), question, processor, "single_frame_verified_v8_local"
    )
    assert old != supplement_request_id(store.read_version(version), question, processor)
    prefix = f"supplement-{old}-{plan['plan_id']}-{plan['windows'][0]['ordinal']:04d}-00-extract"
    (store.base / f"{prefix}.dispatched.json").write_text("{}")
    (store.base / f"{prefix}.raw.json").write_text(
        json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"}))
    )
    result = compile_local_supplement(store, question, plan, processor, lambda: True)
    assert any(p.startswith(LOCAL_GENDER_PROMPT) for p in processor.prompts)
    assert not any(p.startswith(LOCAL_COMPACT_PROMPT) for p in processor.prompts)
    assert store.read_version(result).supplements[-1].request_fingerprint == old


def test_local_v6_resume_stops_if_v7_dispatch_appears_while_waiting(tmp_path):
    from contextlib import contextmanager

    store, version, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    processor.fixture_only = False
    processor.local_only = True
    state = store.read_version(version)
    old = supplement_request_id(state, question, processor, "single_frame_verified_v6")
    current = supplement_request_id(state, question, processor)
    prefix = f"supplement-{old}-{plan['plan_id']}-{plan['windows'][0]['ordinal']:04d}-00-extract"
    (store.base / f"{prefix}.dispatched.json").write_text("{}")
    (store.base / f"{prefix}.raw.json").write_text(
        json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"}))
    )

    @contextmanager
    def competing_v7():
        (
            store.base / f"supplement-{current}-other-plan-0000-00-extract.dispatched.json"
        ).write_text("{}")
        yield

    with pytest.raises(ValueError, match="knowledge_supplement_receipts_plan_conflict"):
        compile_local_supplement(
            store, question, plan, processor, lambda: True, call_scope=competing_v7
        )
    assert processor.calls == 0


def test_changed_plan_cannot_bypass_unknown_or_repeat_completed_receipt(tmp_path):
    store, version, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    request = supplement_request_id(store.read_version(version), question, processor)
    marker = store.base / f"supplement-{request}-other-plan-0000-00-extract.dispatched.json"
    marker.write_text("{}")
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    assert processor.calls == 0
    raw = marker.with_name(marker.name.replace(".dispatched.json", ".raw.json"))
    raw.write_text(json.dumps({"text": "{}"}))
    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    raw.write_text(json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"})))
    with pytest.raises(ValueError, match="knowledge_supplement_receipts_plan_conflict"):
        compile_local_supplement(store, question, plan, processor, lambda: True)
    assert processor.calls == 0


def test_cross_plan_receipt_created_while_waiting_is_not_resent(tmp_path):
    from contextlib import contextmanager

    store, version, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    request = supplement_request_id(store.read_version(version), question, processor)

    @contextmanager
    def another_plan_wins():
        marker = store.base / f"supplement-{request}-other-plan-0000-00-extract.dispatched.json"
        marker.write_text("{}")
        yield

    with pytest.raises(ValueError, match="knowledge_supplement_receipts_plan_conflict"):
        compile_local_supplement(
            store, question, plan, processor, lambda: True, call_scope=another_plan_wins
        )
    assert processor.calls == 0


def test_same_plan_unknown_receipt_created_while_waiting_stops_dispatch(tmp_path):
    from contextlib import contextmanager

    store, version, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    request = supplement_request_id(store.read_version(version), question, processor)

    @contextmanager
    def unresolved_other_frame():
        marker = (
            store.base / f"supplement-{request}-{plan['plan_id']}-0000-01-extract.dispatched.json"
        )
        marker.write_text("{}")
        yield

    with pytest.raises(ValueError, match="knowledge_dispatch_unknown"):
        compile_local_supplement(
            store, question, plan, processor, lambda: True, call_scope=unresolved_other_frame
        )
    assert processor.calls == 0


def test_cancel_after_staging_preserves_published_index_and_reuses_receipts(tmp_path, monkeypatch):
    store, original, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    running = True
    checkpoint = (store.base / "checkpoint.json").read_bytes()
    active_pointer = (store.base / "active.json").read_bytes()
    stage = store.stage_supplement

    def stage_then_cancel(*args):
        nonlocal running
        version = stage(*args)
        running = False
        return version

    monkeypatch.setattr(store, "stage_supplement", stage_then_cancel)
    with pytest.raises(ValueError, match="knowledge_cancelled"):
        compile_local_supplement(store, question, plan, processor, lambda: running)
    assert (store.base / "checkpoint.json").read_bytes() == checkpoint
    assert (store.base / "active.json").read_bytes() == active_pointer
    assert store.read_version(original).publishable_index().frames
    calls = processor.calls
    running = True
    monkeypatch.setattr(store, "stage_supplement", stage)
    version = compile_local_supplement(store, question, plan, processor, lambda: running)
    assert version != original and processor.calls == calls
    assert (store.base / "checkpoint.json").read_bytes() == checkpoint
    assert (store.base / "active.json").read_bytes() == active_pointer


def test_competing_supplements_stage_from_the_same_published_version(tmp_path):
    store, original, question, plan = ready(tmp_path)
    state = store.read_version(original)
    other_question = "find people wearing yellow hat"
    other_plan = plan_supplement(state.publishable_index(), other_question, original)
    first = compile_local_supplement(store, question, plan, FixtureSupplement(), lambda: True)
    second = compile_local_supplement(
        store, other_question, other_plan, FixtureSupplement(), lambda: True
    )
    assert first != second and first != original and second != original
    assert store.read_version(original) == state
    assert len(store.read_version(first).supplements) == 1
    assert len(store.read_version(second).supplements) == 1


def test_supplement_rejects_modified_plan_and_nonfixture_processor(tmp_path):
    store, _, question, plan = ready(tmp_path)
    processor = FixtureSupplement()
    with pytest.raises(ValueError, match="plan_changed"):
        compile_local_supplement(store, question, plan | {"windows": []}, processor, lambda: True)
    assert processor.calls == 0
    processor.fixture_only = False
    with pytest.raises(ValueError, match="local_processor_required"):
        compile_local_supplement(store, question, plan, processor, lambda: True)


def test_description_does_not_supply_missing_subject_kind(tmp_path):
    store, _, question, plan = ready(tmp_path)

    class Person(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            self.calls += 1
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "yes"})}
            return {
                "text": json.dumps(
                    {
                        "frames": [
                            {
                                "index": "0",
                                "description": "A person wearing a red hat.",
                                "subjects": [
                                    {
                                        "id": "one",
                                        "facts": [
                                            {"field": "garment", "value": "hat"},
                                            {"field": "color", "value": "red"},
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                )
            }

    processor = Person()
    version = compile_local_supplement(store, question, plan, processor, lambda: True)
    from frameseek.semantic_search import resolve_conditions

    index = store.read_version(version).publishable_index()
    conditions, problems = resolve_conditions(index, question)
    assert not problems
    result = search_occurrences(index, [condition.model_dump() for condition in conditions])
    assert not result["occurrences"]
    assert result["coverage"]["uncertain_frame_ids"]


def test_uncertain_subjects_are_not_published_as_matches(tmp_path):
    store, original, question, plan = ready(tmp_path)

    class Uncertain(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "uncertain"})}
            return super().infer(prompt, paths, schema)

    version = compile_local_supplement(store, question, plan, Uncertain(), lambda: True)
    assert version != original
    index = store.read_version(version).publishable_index()
    criteria = [
        {"field": "kind", "value": "person"},
        {"field": "color", "value": "red"},
        {"field": "garment", "value": "hat"},
    ]
    assert not search_occurrences(index, criteria)["occurrences"]


def test_named_accessory_color_is_bound_to_that_accessory(tmp_path):
    store, _, _, _ = ready(tmp_path)
    question = "find people wearing blue jersey and yellow headband"
    state = store.load()
    version = fingerprint(state.model_dump(mode="json"))
    plan = plan_supplement(state.publishable_index(), question, version)

    class Person(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "yes"})}
            return {
                "text": json.dumps(
                    {
                        "frames": [
                            {
                                "index": "0",
                                "description": "A person in a blue jersey and yellow headband.",
                                "subjects": [
                                    {
                                        "id": "one",
                                        "facts": [
                                            {"field": "kind", "value": "person"},
                                            {"field": "garment", "value": "jersey"},
                                            {"field": "color", "value": "blue"},
                                            {"field": "accessory", "value": "headband"},
                                            {"field": "headband_color", "value": "yellow"},
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                )
            }

    new_version = compile_local_supplement(store, question, plan, Person(), lambda: True)
    from frameseek.semantic_search import resolve_conditions

    index = store.read_version(new_version).publishable_index()
    conditions, problems = resolve_conditions(index, question)
    assert not problems
    assert search_occurrences(index, [condition.model_dump() for condition in conditions])[
        "occurrences"
    ]


def test_generic_color_is_not_assigned_to_garment_when_other_item_is_unbound(tmp_path):
    store, _, _, _ = ready(tmp_path)
    question = "find people wearing blue jersey"
    state = store.load()
    version = fingerprint(state.model_dump(mode="json"))
    plan = plan_supplement(state.publishable_index(), question, version)

    class Ambiguous(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "yes"})}
            facts = [
                {"field": "kind", "value": "person"},
                {"field": "garment", "value": "jersey"},
                {"field": "color", "value": "blue"},
                {"field": "headwear", "value": "hat"},
            ]
            return {
                "text": json.dumps(
                    {
                        "frames": [
                            {
                                "index": "0",
                                "description": "person with clothing",
                                "subjects": [{"id": "one", "facts": facts}],
                            }
                        ]
                    }
                )
            }

    updated = compile_local_supplement(store, question, plan, Ambiguous(), lambda: True)
    for row in store.read_version(updated).supplements[-1].observations:
        assert "blue jersey" not in row.subjects[0].facts["garment"]


@pytest.mark.parametrize("held", ["holding basketball", "holding umbrella"])
def test_generic_color_is_not_assigned_to_garment_when_holding_an_object(tmp_path, held):
    store, _, _, _ = ready(tmp_path)
    question = "find people wearing blue jersey"
    state = store.load()
    version = fingerprint(state.model_dump(mode="json"))
    plan = plan_supplement(state.publishable_index(), question, version)

    class Ambiguous(FixtureSupplement):
        def infer(self, prompt, paths, schema):
            if "visible" in schema.get("properties", {}):
                return {"text": json.dumps({"visible": "yes"})}
            return {
                "text": json.dumps(
                    {
                        "frames": [
                            {
                                "index": "0",
                                "description": "person holding an object",
                                "subjects": [
                                    {
                                        "id": "one",
                                        "facts": [
                                            {"field": "kind", "value": "person"},
                                            {"field": "garment", "value": "jersey"},
                                            {"field": "color", "value": "blue"},
                                            {"field": "action", "value": held},
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                )
            }

    updated = compile_local_supplement(store, question, plan, Ambiguous(), lambda: True)
    for row in store.read_version(updated).supplements[-1].observations:
        assert "blue jersey" not in row.subjects[0].facts["garment"]
