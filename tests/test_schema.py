import copy

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JSONSchemaError
from pydantic import ValidationError

from frameseek.fixtures import examples
from frameseek.schema import Answer, QueryControl, hms


def test_all_four_fixtures_round_trip_with_required_nullable_fields():
    validator = Draft202012Validator(Answer.model_json_schema())
    for answer in examples().values():
        data = answer.model_dump()
        validator.validate(data)
        assert {"schema_version", "timebase", "trace", "errors", "followups"} <= data.keys()
        assert data["trace"]["debug"] is None
        if data["events"]:
            assert "support_level" in data["events"][0]
            assert "support_score" not in data["events"][0]
        assert Answer.model_validate_json(answer.model_dump_json()) == answer


@pytest.mark.parametrize(
    "status,field,value",
    [
        ("ok", "events", []),
        ("ok", "unresolved", ["unknown"]),
        ("partial", "events", []),
        ("partial", "unresolved", []),
        ("no_result", "events", ["fake"]),
        ("no_result", "unresolved", ["failed"]),
        ("error", "errors", []),
        ("no_result", "timebase", "window_offset"),
    ],
)
def test_invalid_status_contracts(status, field, value):
    data = examples()[status].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        Answer.model_validate(data)
    with pytest.raises(JSONSchemaError):
        Draft202012Validator(Answer.model_json_schema()).validate(data)


def test_json_schema_enforces_support_key_exclusion():
    data = examples()["ok"].model_dump()
    data["events"][0]["support_score"] = None
    with pytest.raises(ValidationError):
        Answer.model_validate(data)
    with pytest.raises(JSONSchemaError):
        Draft202012Validator(Answer.model_json_schema()).validate(data)


@pytest.mark.parametrize("support_field", ["support_level", "support_score"])
def test_json_schema_rejects_unmeasured_support_as_null(support_field):
    data = examples()["ok"].model_dump()
    data["events"][0].pop("support_level")
    data["events"][0][support_field] = None
    with pytest.raises(ValidationError):
        Answer.model_validate(data)
    with pytest.raises(JSONSchemaError):
        Draft202012Validator(Answer.model_json_schema()).validate(data)


@pytest.mark.parametrize("state,interval", [("pending", None), ("pending", 1), ("unknown", 2)])
def test_recovery_json_schema_rejects_invalid_polling(state, interval):
    data = dict(
        kind="query_control",
        state=state,
        request_id="r1",
        suggested_action="Recover same request",
        retry_after_seconds=interval,
    )
    with pytest.raises(ValidationError):
        QueryControl.model_validate(data)
    with pytest.raises(JSONSchemaError):
        Draft202012Validator(QueryControl.model_json_schema()).validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"support_score": 0.72},
        {"support_level": None},
        {"end": 1},
        {"start": float("nan")},
        {"start_hms": "00:01:00"},
        {"video_id": "other"},
    ],
)
def test_invalid_evidence_support_and_times(mutation):
    data = copy.deepcopy(examples()["ok"].model_dump())
    data["events"][0].update(mutation)
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


def test_out_of_range_evidence_and_hidden_trace_rejected():
    data = examples()["ok"].model_dump()
    data["events"][0]["evidence"][0].update(t=99, t_hms=hms(99))
    with pytest.raises(ValidationError):
        Answer.model_validate(data)
    data = examples()["ok"].model_dump()
    data["trace"]["debug"] = {"reasoning": "private"}
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


def test_recovery_is_not_a_fifth_answer_and_unknown_stops_polling():
    pending = dict(
        kind="query_control",
        state="pending",
        request_id="r1",
        suggested_action="Wait for same request",
        retry_after_seconds=2,
    )
    control = QueryControl.model_validate(pending)
    assert "status" not in control.model_dump()
    with pytest.raises(ValidationError):
        Answer.model_validate(pending)
    with pytest.raises(ValidationError):
        QueryControl.model_validate({**pending, "state": "unknown"})
    assert QueryControl.model_validate({**pending, "state": "unknown", "retry_after_seconds": None})


def test_milliseconds_carry_and_non_finite_numbers_rejected():
    assert hms(59.9996) == "00:01:00"
    assert hms(2541.5) == "00:42:21.5"
    data = examples()["ok"].model_dump()
    data["events"][0]["evidence"][0]["t"] = True
    with pytest.raises(ValidationError):
        Answer.model_validate(data)
