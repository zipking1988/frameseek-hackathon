import pytest

from frameseek.score_transitions import ScoreReading, propose_score_changes


def readings(scores=(100, 100, 103, 103)):
    return [
        ScoreReading(
            t=float(i),
            segment="continuous-A",
            period="fourth",
            clock_remaining=80.0 - i,
            scores={"left": score, "right": 103},
            trusted=True,
        )
        for i, score in enumerate(scores)
    ]


def test_stable_score_increase_is_only_a_verification_proposal():
    result = propose_score_changes(readings(), 3)
    assert len(result) == 1
    assert result[0]["start"] == 1
    assert result[0]["end"] == 2
    assert result[0]["score_updated_by"] == 3
    assert result[0]["verdict"] == "requires_action_and_replay_verification"
    assert result[0]["evidence_times"] == [0, 1, 2, 3]


def test_delta_is_source_task_configuration_not_basketball_hardcoding():
    assert len(propose_score_changes(readings((4, 4, 5, 5)), 1)) == 1
    assert propose_score_changes(readings((4, 4, 5, 5)), 3) == []


def test_unstable_ocr_and_corrections_do_not_become_events():
    assert propose_score_changes(readings((100, 103, 100, 100)), 3) == []
    assert propose_score_changes(readings((103, 103, 100, 100)), 3) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("segment", "replay"),
        ("period", "overtime"),
        ("clock_remaining", 200.0),
        ("trusted", False),
        ("t", 12.0),
    ],
)
def test_cut_period_clock_reset_untrusted_or_gap_prevents_bridging(field, value):
    data = readings()
    data[2] = data[2].model_copy(update={field: value})
    if field == "t":
        data[3] = data[3].model_copy(update={"t": 13.0})
    assert propose_score_changes(data, 3) == []


def test_changed_team_labels_or_both_scores_do_not_infer_a_single_scoring_action():
    data = readings()
    for i in [2, 3]:
        data[i] = data[i].model_copy(update={"scores": {"left": 103, "right": 105}})
    assert propose_score_changes(data, 3) == []
    data[2] = data[2].model_copy(update={"scores": {"new": 103, "right": 103}})
    assert propose_score_changes(data, 3) == []


def test_non_monotonic_input_and_invalid_parameters_rejected():
    with pytest.raises(ValueError, match="increasing"):
        propose_score_changes(readings()[::-1], 3)
    with pytest.raises(ValueError, match="parameters"):
        propose_score_changes(readings(), True)
