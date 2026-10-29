"""Score-change proposals from reviewed/OCR readings, never made-shot counts.

This module does not read pixels. A stable delta only proposes an interval for
continuous-action verification; it cannot distinguish a shot from free throws,
corrections, edited footage or repeated broadcast coverage.
"""

from pydantic import Field

from frameseek.evaluation import Artifact


class ScoreReading(Artifact):
    t: float = Field(ge=0)
    segment: str = Field(min_length=1)
    period: str = Field(min_length=1)
    clock_remaining: float = Field(ge=0)
    scores: dict[str, int] = Field(min_length=2)
    trusted: bool


def propose_score_changes(readings, delta, max_gap=2.0):
    if type(delta) is not int or delta <= 0 or not 0 < max_gap <= 10:
        raise ValueError("invalid score-change parameters")
    if any(b.t <= a.t for a, b in zip(readings, readings[1:], strict=False)):
        raise ValueError("score readings must have increasing source timestamps")
    runs = []
    proposals = []
    for reading in readings:
        previous = runs[-1][-1] if runs else None
        continuous = (
            previous is not None
            and reading.trusted
            and previous.trusted
            and reading.segment == previous.segment
            and reading.period == previous.period
            and reading.scores.keys() == previous.scores.keys()
            and reading.t - previous.t <= max_gap
            and reading.clock_remaining <= previous.clock_remaining
        )
        if not reading.trusted or any(v < 0 for v in reading.scores.values()):
            runs = []
            continue
        if not continuous:
            runs = [[reading]]
        elif reading.scores == previous.scores:
            runs[-1].append(reading)
        else:
            runs.append([reading])
        if len(runs) < 2 or len(runs[-2]) < 2 or len(runs[-1]) != 2:
            continue
        before, after = runs[-2][-1], runs[-1][0]
        changes = {key: after.scores[key] - before.scores[key] for key in before.scores}
        changed = [(key, value) for key, value in changes.items() if value]
        if len(changed) == 1 and changed[0][1] == delta:
            proposals.append(
                {
                    "start": before.t,
                    "end": after.t,
                    "score_updated_by": reading.t,
                    "side": changed[0][0],
                    "delta": delta,
                    "before": before.scores,
                    "after": after.scores,
                    "evidence_times": [r.t for run in runs[-2:] for r in run],
                    "verdict": "requires_action_and_replay_verification",
                }
            )
        runs = runs[-2:]
    return proposals
