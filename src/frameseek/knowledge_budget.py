"""Read-only preflight for a future paid VideoKnowledge extractor.

Does not install approvals or dispatch calls. Each real call still needs the
provider's atomic reservation; a successful preflight is not a reservation.
"""

import json
import math
import re
import time
from dataclasses import dataclass

from frameseek.attempts import BudgetDenied, finite_integer

BASE_CONTRACT_CONFIG = {
    "video_knowledge_extraction_v3": "knowledge-v3-8sec-8frames-4096tokens",
    "video_knowledge_extraction_v4": "knowledge-v4-bound-8sec-8frames-4096tokens",
    "video_knowledge_extraction_v5": "knowledge-v5-objects-8sec-8frames-4096tokens",
}


@dataclass(frozen=True)
class ExtractionCalibration:
    reference: str
    processor_revision: str
    contract: str
    max_frames: int
    max_output_tokens: int
    max_cost_units: int
    currency_unit: str
    valid_until: float


def admit_base_index(ledger, owner, runtime, calibration, *, completed_windows, total_windows):
    """Reject uncalibrated, expired or unaffordable builds before any dispatch."""
    finite_integer(completed_windows, "completed_windows")
    finite_integer(total_windows, "total_windows", minimum=1)
    if total_windows > 450 or completed_windows > total_windows:
        raise BudgetDenied("Invalid knowledge window count")
    if (
        not runtime
        or runtime.blockers()
        or runtime.provider != "tokenfactory"
        or not runtime.allow_tokenfactory_media
        or calibration is None
    ):
        raise BudgetDenied("Knowledge extraction is not calibrated or configured")
    finite_integer(calibration.max_frames, "calibration_max_frames", minimum=1)
    finite_integer(calibration.max_output_tokens, "calibration_max_output_tokens", minimum=1)
    finite_integer(calibration.max_cost_units, "calibration_max_cost", minimum=1)
    if (
        not isinstance(calibration.reference, str)
        or not calibration.reference.strip()
        or calibration.contract not in BASE_CONTRACT_CONFIG
        or calibration.processor_revision != runtime.vision.revision
        or calibration.max_frames < 8
        or calibration.max_output_tokens < 4096
        or calibration.currency_unit != runtime.currency_unit
        or not re.fullmatch(r"[A-Z]{3}_MICRO", calibration.currency_unit)
        or type(calibration.valid_until) not in (int, float)
        or not math.isfinite(calibration.valid_until)
        or calibration.valid_until <= time.time()
        or calibration.valid_until > runtime.valid_until
        or calibration.max_cost_units > runtime.vision.max_cost_micro
    ):
        raise BudgetDenied("Knowledge extraction calibration does not match runtime")
    calls = total_windows - completed_windows
    maximum = calls * runtime.vision.max_cost_micro
    finite_integer(maximum, "knowledge_maximum_cost")
    # Both scopes and active approval must be checked even for a cached resume.
    ledger.admit_windows(owner, runtime.budget_version, calls)
    ledger.admit_cost(owner, runtime.budget_version, calls, maximum, runtime.currency_unit)
    return {
        "total_windows": total_windows,
        "completed_windows": completed_windows,
        "new_calls": calls,
        "maximum_cost_units": maximum,
        "currency_unit": runtime.currency_unit,
        "calibration_reference": calibration.reference,
        "model_requests_sent": 0,
    }


def admit_base_index_store(ledger, owner, runtime, calibration, store):
    """Estimate from validated checkpoints and durable replies, never caller counts.

    This is a preflight, not a reservation. An ambiguous dispatched window is
    rejected even when the remaining approval could cover another attempt.
    """
    from frameseek.index_service import source_digest
    from frameseek.knowledge_compiler import packet_to_segment
    from frameseek.video_knowledge import validate_global_sampling

    with store.locked():
        state = store._load()
        if (
            source_digest(store.directory) != state.source_fingerprint
            or not runtime
            or state.processor_revision != runtime.vision.revision
            or calibration is None
            or state.extraction_config != BASE_CONTRACT_CONFIG.get(calibration.contract)
        ):
            raise BudgetDenied("Knowledge checkpoint identity does not match extractor")
        total = math.ceil(state.duration / 8)
        for segment in state.segments:
            for frame in segment.frames:
                path = store.directory / "frames" / frame.file
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.resolve().parent != (store.directory / "frames").resolve()
                ):
                    raise BudgetDenied("Knowledge checkpoint evidence missing")
        validated = len(state.segments)
        for ordinal in state.pending():
            raw = store.base / f"window-{ordinal:04d}.raw.json"
            decoded = store.base / f"window-{ordinal:04d}.decoded.json"
            marker = store.base / f"window-{ordinal:04d}.dispatched.json"
            if raw.exists() or raw.is_symlink():
                if not (decoded.exists() or decoded.is_symlink()):
                    raise BudgetDenied("Knowledge reply has no decoded frame record")
                try:
                    rows = json.loads(store._read(decoded))
                    reply = json.loads(store._read(raw))
                    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
                        raise ValueError("invalid_decoded_frames")
                    for frame in rows:
                        path = store.directory / "frames" / frame["file"]
                        if (
                            path.is_symlink()
                            or not path.is_file()
                            or path.resolve().parent != (store.directory / "frames").resolve()
                        ):
                            raise ValueError("knowledge_evidence_missing")
                    state = state.checkpoint(packet_to_segment(state, ordinal, rows, reply))
                except (OSError, ValueError, TypeError, KeyError) as error:
                    raise BudgetDenied("Invalid cached knowledge reply") from error
                validated += 1
            elif marker.exists() or marker.is_symlink():
                raise BudgetDenied("Knowledge dispatch outcome unknown")
        if state.segments:
            try:
                validate_global_sampling(state.observation_index())
            except ValueError as error:
                raise BudgetDenied("Knowledge checkpoint sampling exceeds its limit") from error
        return admit_base_index(
            ledger, owner, runtime, calibration, completed_windows=validated, total_windows=total
        )


def admit_supplement_store(ledger, owner, runtime, calibration, store, question, plan, processor):
    """Bound all missing per-frame extraction and verification calls from durable receipts."""
    from frameseek.cloud_vision import validate_shape
    from frameseek.index_service import source_digest
    from frameseek.knowledge_compiler import extraction_schema
    from frameseek.knowledge_planning import plan_supplement
    from frameseek.knowledge_supplement import (
        VERIFY_SCHEMA,
        legacy_supplement_unknown,
        supplement_receipts_cross_plan,
        supplement_request_id,
    )
    from frameseek.local_vision import json_response
    from frameseek.occurrences import fingerprint

    if not runtime or runtime.blockers() or runtime.provider != "tokenfactory" or not calibration:
        raise BudgetDenied("Knowledge supplement is not calibrated or configured")
    finite_integer(calibration.max_frames, "calibration_max_frames", minimum=1)
    finite_integer(calibration.max_output_tokens, "calibration_max_output_tokens", minimum=1)
    finite_integer(calibration.max_cost_units, "calibration_max_cost", minimum=1)
    if (
        not isinstance(calibration.reference, str)
        or not calibration.reference.strip()
        or calibration.contract != "video_knowledge_supplement_v6"
        or calibration.processor_revision != runtime.vision.revision
        or calibration.max_frames < 1
        or calibration.max_output_tokens < 4096
        or calibration.currency_unit != runtime.currency_unit
        or type(calibration.valid_until) not in (int, float)
        or not math.isfinite(calibration.valid_until)
        or not time.time() < calibration.valid_until <= runtime.valid_until
        or calibration.max_cost_units > runtime.vision.max_cost_micro
    ):
        raise BudgetDenied("Knowledge supplement calibration does not match runtime")
    state = store.read_version(plan["knowledge_version"])
    with store.locked():
        if legacy_supplement_unknown(store, state, question, processor):
            raise BudgetDenied("Knowledge supplement dispatch outcome unknown")
        version = fingerprint(state.model_dump(mode="json"))
        if (
            source_digest(store.directory) != state.source_fingerprint
            or plan != plan_supplement(state.publishable_index(), question, version)
            or plan["kind"] != "supplement_candidates"
        ):
            raise BudgetDenied("Knowledge supplement source or plan changed")
        request_id = supplement_request_id(state, question, processor)
        if any(item.request_fingerprint == request_id for item in state.supplements):
            ledger.admit_cost(owner, runtime.budget_version, 0, 0, runtime.currency_unit)
            return {"new_calls": 0, "maximum_cost_units": 0, "model_requests_sent": 0}
        if supplement_receipts_cross_plan(store, request_id, plan["plan_id"]):
            raise BudgetDenied("Knowledge supplement receipt mapping unknown across plans")
        frames = {f.frame_id: f for f in state.publishable_index().frames}
        calls = 0
        for window in plan["windows"]:
            for position, frame_id in enumerate(window["frame_ids"]):
                frame = frames[frame_id]
                path = store.directory / "frames" / frame.file
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.resolve().parent != (store.directory / "frames").resolve()
                ):
                    raise BudgetDenied("Knowledge supplement evidence missing")
                prefix = (
                    f"supplement-{request_id}-{plan['plan_id']}-"
                    f"{window['ordinal']:04d}-{position:02d}"
                )
                responses = {}
                for suffix, schema in (
                    ("extract", extraction_schema(1)),
                    ("verify", VERIFY_SCHEMA),
                ):
                    raw = store.base / f"{prefix}-{suffix}.raw.json"
                    marker = store.base / f"{prefix}-{suffix}.dispatched.json"
                    if raw.exists() or raw.is_symlink():
                        try:
                            parsed = json_response(json.loads(store._read(raw))["text"])
                            validate_shape(parsed, schema)
                            if suffix == "extract" and parsed["frames"][0]["index"] != "0":
                                raise ValueError("invalid_frame")
                            responses[suffix] = parsed
                        except (OSError, ValueError, TypeError, KeyError) as error:
                            raise BudgetDenied("Invalid cached supplement reply") from error
                    elif marker.exists() or marker.is_symlink():
                        raise BudgetDenied("Knowledge supplement dispatch outcome unknown")
                if "extract" not in responses:
                    calls += 2
                elif responses["extract"]["frames"][0]["subjects"] and "verify" not in responses:
                    calls += 1
        maximum = calls * runtime.vision.max_cost_micro
        finite_integer(maximum, "knowledge_maximum_cost")
        ledger.admit_cost(owner, runtime.budget_version, calls, maximum, runtime.currency_unit)
        return {"new_calls": calls, "maximum_cost_units": maximum, "model_requests_sent": 0}
