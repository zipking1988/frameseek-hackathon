"""Schema 0.1: final answers and query recovery are separate contracts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

FiniteSeconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]
Text = Annotated[str, Field(min_length=1, max_length=10000)]


def hms(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, fractional = divmod(remainder, 1000)
    suffix = f".{fractional:03d}".rstrip("0") if fractional else ""
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{suffix}"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ErrorDetail(Contract):
    code: Identifier
    stage: Text
    message: Text
    cause: Text
    retryable: bool
    suggested_action: Text
    docs_url: Literal["/docs/errors"]
    request_id: Identifier


class Evidence(Contract):
    t: FiniteSeconds
    t_hms: Text
    frame_id: Identifier
    uri: Annotated[str, Field(pattern=r"^/media/[a-zA-Z0-9_-]+/frames/[a-zA-Z0-9_.-]+\.jpg$")]
    caption: Text | None
    asr: Text | None

    @model_validator(mode="after")
    def check_evidence(self) -> "Evidence":
        if self.t_hms != hms(self.t):
            raise ValueError("Evidence human time must match absolute time")
        if self.caption is None and self.asr is None:
            raise ValueError("Evidence requires observable caption or source speech")
        return self


class Event(Contract):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["support_level"],
                    "properties": {"support_level": {"type": "string"}},
                    "not": {"required": ["support_score"]},
                },
                {
                    "required": ["support_score"],
                    "properties": {"support_score": {"type": "number"}},
                    "not": {"required": ["support_level"]},
                },
            ]
        }
    )
    video_id: Identifier
    start: FiniteSeconds
    end: FiniteSeconds
    start_hms: Text
    end_hms: Text
    action: Text
    summary: Text
    support_score: Annotated[float, Field(ge=0, le=1)] | None = None
    support_level: Literal["high", "medium", "low"] | None = None
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=128)]

    @model_validator(mode="before")
    @classmethod
    def reject_two_support_keys(cls, values):
        if isinstance(values, dict) and "support_score" in values and "support_level" in values:
            raise ValueError("Supply only one support field, including nullable fields")
        return values

    @model_serializer(mode="wrap")
    def serialize_support(self, handler):
        data = handler(self)
        for key in ("support_score", "support_level"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def check_event(self) -> "Event":
        if self.end <= self.start:
            raise ValueError("Event end must follow start")
        if (self.support_score is None) == (self.support_level is None):
            raise ValueError("Exactly one support field must be present")
        if self.start_hms != hms(self.start) or self.end_hms != hms(self.end):
            raise ValueError("Event human times must match absolute times")
        for item in self.evidence:
            if not self.start <= item.t <= self.end:
                raise ValueError("Evidence must lie within the event")
            if not item.uri.startswith(f"/media/{self.video_id}/frames/"):
                raise ValueError("Evidence must belong to the event video")
            if not item.frame_id.startswith(self.video_id + "_"):
                raise ValueError("Frame identifier must belong to the event video")
        return self


class VisibleTrace(Contract):
    channels: list[Literal["asr_keyword", "visual_vector"]]
    segments_reviewed: Annotated[int, Field(ge=0)]
    evidence_segments: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def check_counts(self) -> "VisibleTrace":
        if self.evidence_segments > self.segments_reviewed:
            raise ValueError("Evidence count cannot exceed reviewed count")
        return self


class Trace(Contract):
    visible: VisibleTrace
    debug: None = None  # D1 public contract never exposes internal reasoning or tool inputs.


class Answer(Contract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": state}}, "required": ["status"]},
                    "then": {"properties": rules},
                }
                for state, rules in {
                    "ok": {
                        "events": {"minItems": 1},
                        "unresolved": {"maxItems": 0},
                        "errors": {"maxItems": 0},
                    },
                    "partial": {"events": {"minItems": 1}, "unresolved": {"minItems": 1}},
                    "no_result": {
                        "events": {"maxItems": 0},
                        "unresolved": {"maxItems": 0},
                        "errors": {"maxItems": 0},
                    },
                    "error": {"events": {"maxItems": 0}, "errors": {"minItems": 1}},
                }.items()
            ]
        }
    )
    schema_version: Literal["0.1"]
    status: Literal["ok", "partial", "no_result", "error"]
    timebase: Literal["video_absolute_seconds"]
    answer: Text
    events: Annotated[list[Event], Field(max_length=100)]
    unresolved: Annotated[list[Text], Field(max_length=100)]
    followups: Annotated[list[Text], Field(max_length=10)]
    trace: Trace
    errors: Annotated[list[ErrorDetail], Field(max_length=100)]

    @model_validator(mode="after")
    def check_status(self) -> "Answer":
        if self.status == "ok" and (not self.events or self.unresolved or self.errors):
            raise ValueError("ok requires valid events and no unresolved/error items")
        if self.status == "partial" and (not self.events or not self.unresolved):
            raise ValueError("partial requires valid events and unresolved items")
        if self.status == "no_result" and (self.events or self.unresolved or self.errors):
            raise ValueError("no_result is successful processing with no matching evidence")
        if self.status == "error" and (self.events or not self.errors):
            raise ValueError("error requires failure details and no valid events")
        return self


class QueryControl(Contract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "pending"}}, "required": ["state"]},
                    "then": {
                        "required": ["retry_after_seconds"],
                        "properties": {"retry_after_seconds": {"type": "integer"}},
                    },
                },
                {
                    "if": {"properties": {"state": {"const": "unknown"}}, "required": ["state"]},
                    "then": {"properties": {"retry_after_seconds": {"type": "null"}}},
                },
            ]
        }
    )
    kind: Literal["query_control"]
    state: Literal["pending", "unknown"]
    request_id: Identifier
    suggested_action: Text
    retry_after_seconds: Annotated[int, Field(ge=2, le=10)] | None = None

    @model_validator(mode="after")
    def check_polling(self) -> "QueryControl":
        if self.state == "pending" and self.retry_after_seconds is None:
            raise ValueError("pending must provide bounded polling interval")
        if self.state == "unknown" and self.retry_after_seconds is not None:
            raise ValueError("unknown must stop automatic polling")
        return self
