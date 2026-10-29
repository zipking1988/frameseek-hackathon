import json

import pytest

from frameseek.question_context import resolve_followup
from frameseek.score_events import answer_scores, candidates, reading, score_target


def ocr(t, a=100, b=103, clock="1:22", split=False):
    def row(text, x):
        return {"text": text, "confidence": 1.0, "x": x, "y": 0.3, "w": 0.04, "h": 0.15}

    rows = [row("LEFT", 0.2), row(str(a), 0.25)] if split else [row(f"LEFT {a}", 0.2)]
    return {
        "frame": {
            "t": float(t),
            "file": f"f{t}.jpg",
            "frame_id": f"v_f{t}",
            "uri": f"/media/v/frames/f{t}.jpg",
        },
        "rows": rows + [row(f"RIGHT {b}", 0.4), row("4th", 0.55), row(clock, 0.6)],
    }


def test_dynamic_labels_split_ocr_and_subminute_clock_are_supported():
    for split in [False, True]:
        x = ocr(2, clock="56.6", split=split)
        r = reading(x["frame"], x["rows"])
        assert r.scores == {"LEFT": 100, "RIGHT": 103}
        assert r.clock_remaining == 56.6
    bad = ocr(2)
    bad["rows"][0]["confidence"] = 0.1
    assert reading(bad["frame"], bad["rows"]) is None


def test_candidate_is_not_created_for_two_points_or_clock_reset():
    xs = [ocr(0), ocr(1), ocr(2, a=103), ocr(3, a=103)]
    assert len(candidates(xs, 3)) == 1
    assert candidates([ocr(0), ocr(1), ocr(2, a=102), ocr(3, a=102)], 3) == []
    xs[2] = ocr(2, a=103, clock="2:00")
    assert candidates(xs, 3) == []


def test_continuation_restores_original_intent_without_overwriting_a_new_question():
    assert resolve_followup("继续这个问题", {"question": "多少个3分球投入"}) == "多少个3分球投入"
    assert resolve_followup("哪里有白衣服？", {"question": "多少个3分球投入"}) == "哪里有白衣服？"
    assert "棕色箱子" in resolve_followup("它出现在哪些时间？", {"question": "有没有棕色箱子？"})
    assert (
        resolve_followup("When does it appear?", {"question": "Is there a box?"})
        == "Is there a box?, all times"
    )
    assert "white jersey" in resolve_followup(
        "Where did he appear?", {"question": "find a person wearing a white jersey"}
    )
    with pytest.raises(ValueError, match="followup_context_ambiguous"):
        resolve_followup("When does he appear?", {"question": "Is there a box?"})
    prior = "请在05:04至05:12找出穿蓝色球衣并戴黄色头带的球员，列出出现时间和画面。"
    assert "05:04至05:12" in resolve_followup("他出现在哪些时间？", {"question": prior})
    assert "05:04至05:12" in resolve_followup("他们什么时候出现？", {"question": prior})
    assert "在05:09" in resolve_followup(
        "他出现在哪些时间？", {"question": "在05:09找出穿蓝色球衣的人"}
    )
    assert "white jersey" in resolve_followup(
        "When do they appear?", {"question": "find people wearing a white jersey"}
    )
    english_prior = "Find a box from 00:04 to 00:06"
    assert english_prior in resolve_followup("When does it appear?", {"question": english_prior})
    with pytest.raises(ValueError, match="followup_context_ambiguous"):
        resolve_followup("它出现在哪些时间？", {"question": "箱子和人"})
    with pytest.raises(ValueError, match="followup_context_ambiguous"):
        resolve_followup("他们什么时候出现？", {"question": "箱子和人"})
    with pytest.raises(ValueError):
        resolve_followup("继续这个问题", None)
    assert score_target("多少个3分球投入") == 3
    assert score_target("how many two-point shots?") == 2
    assert score_target("找白衣服") is None


def test_score_records_stay_separate_from_action_verification_and_model_timestamps(
    tmp_path, monkeypatch
):
    import frameseek.score_events as module

    xs = [ocr(0), ocr(1), ocr(2, a=103), ocr(3, a=103)]
    monkeypatch.setattr(module, "scan", lambda *args: xs)
    rows = [{"frames": [x["frame"] for x in xs]}]
    source = {"id": "v", "metadata": '{"duration":4.0}'}

    def write(path, data):
        path.write_text(json.dumps(data))

    class Client:
        confirm = True

        def infer(self, *args, **kwargs):
            return {
                "text": json.dumps(
                    {
                        "confirmed": self.confirm,
                        "is_replay": False,
                        "support_frames": [0, 1],
                        "reason": "untrusted absolute time999",
                    }
                )
            }

    client = Client()
    answer = answer_scores(
        tmp_path,
        source,
        "多少个3分球投入",
        rows,
        tmp_path,
        client,
        lambda m: None,
        lambda: True,
        write,
        json.loads,
    )
    assert len(answer["events"]) == 1 and answer["status"] == "partial"
    assert "00:00:02" in answer["answer"]
    assert all(e["t"] < 4 for event in answer["events"] for e in event["evidence"])
    for p in tmp_path.glob("shot-*"):
        p.unlink()
    client.confirm = False
    denied = answer_scores(
        tmp_path,
        source,
        "多少个3分球投入",
        rows,
        tmp_path,
        client,
        lambda m: None,
        lambda: True,
        write,
        json.loads,
    )
    assert len(denied["events"]) == 1 and denied["status"] == "partial"
    assert denied["events"][0]["action"] == "记分牌增加3分"
    assert "1 次尚未通过" in denied["answer"]
    assert denied["unresolved"]


def test_unknown_verification_is_not_resent(tmp_path, monkeypatch):
    import frameseek.score_events as module

    xs = [ocr(0), ocr(1), ocr(2, a=103), ocr(3, a=103)]
    monkeypatch.setattr(module, "scan", lambda *args: xs)

    class Client:
        calls = 0

        def infer(self, *args, **kwargs):
            self.calls += 1
            raise ValueError("interrupted")

    client = Client()

    def write(path, data):
        path.write_text(json.dumps(data))

    args = (
        tmp_path,
        {"id": "v", "metadata": '{"duration":4.0}'},
        "多少个3分球投入",
        [{"frames": [x["frame"] for x in xs]}],
        tmp_path,
        client,
        lambda m: None,
        lambda: True,
        write,
        json.loads,
    )
    for _ in range(2):
        result = answer_scores(*args)
        assert result["status"] == "partial"
        assert result["events"][0]["action"] == "记分牌增加3分"
    assert client.calls == 1


def test_legacy_continuation_chain_recovers_root_and_rejects_foreign_or_cyclic_context():
    from frameseek.question_context import saved_question

    root = {"id": "root", "video_id": "v", "source_generation": 2, "question": "多少个3分球投入"}
    child = {"id": "child", "video_id": "v", "source_generation": 2, "question": "继续这个问题"}

    def load(key):
        return (root, {}) if key == "root" else None

    assert (
        saved_question(child, {"parent_request_id": "root"}, load, video="v", generation=2)
        == root["question"]
    )
    with pytest.raises(ValueError):
        saved_question(
            child,
            {"parent_request_id": "child"},
            lambda key: (child, {"parent_request_id": "child"}),
            video="v",
            generation=2,
        )
    with pytest.raises(ValueError):
        saved_question(child, {"parent_request_id": "root"}, load, video="other", generation=2)
