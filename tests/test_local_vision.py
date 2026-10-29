"""Local inference lifecycle contracts, using test-only in-process inference."""

import asyncio
import threading

import pytest
from test_index_chat import setup_index_page

from frameseek.schema import Answer


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_local_chat_ownership_idempotence_cancellation_and_zero_paid_calls(
    tmp_path, monkeypatch
):
    import frameseek.local_routes as local

    app, client, video, desk, session, index = await setup_index_page(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(local, "configured", lambda: ("test-interpreter", "test-model"))

    def infer(root, source, question, context, progress, active):
        calls.append(question)
        progress("test-only inference in progress")
        entered.set()
        release.wait(10)
        return Answer.model_validate(
            {
                "schema_version": "0.1",
                "status": "no_result",
                "timebase": "video_absolute_seconds",
                "answer": "explicit test transport",
                "events": [],
                "unresolved": [],
                "followups": [],
                "errors": [],
                "trace": {
                    "visible": {"channels": [], "segments_reviewed": 1, "evidence_segments": 0},
                    "debug": None,
                },
            }
        ).model_dump(mode="json")

    monkeypatch.setattr(local, "answer_source", infer)
    body = {
        "investigation_id": desk["id"],
        "request_id": "local-lifecycle",
        "generation": 0,
        "question": "test-only request",
    }
    try:
        assert (await client.post("/api/local-chat/prepare", json=body)).json()["kind"] == "ready"
        assert (
            await client.post("/api/local-chat", json=body, headers={"x-fs-csrf": "bad"})
        ).status_code == 403
        assert (await client.post("/api/local-chat", json=body)).json()["state"] == "pending"
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert (await client.post("/api/local-chat", json=body)).json()["state"] == "pending"
        assert (
            await client.post("/api/local-chat", json=body | {"question": "changed"})
        ).status_code == 409
        assert len(calls) == 1
        cancelled = await client.post(
            f"/api/investigations/{desk['id']}/requests/local-lifecycle/cancel"
        )
        assert cancelled.status_code == 200
        release.set()
        await asyncio.sleep(0.05)
        result = (await client.post("/api/local-chat", json=body)).json()
        assert result["response"]["status"] == "error" and len(calls) == 1
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        release.set()
        await client.aclose()


def test_actual_local_pipeline_shape_cache_and_reference_validation(tmp_path, monkeypatch):
    import json

    import frameseek.local_vision as local

    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    directory = tmp_path / "vid_local"
    (directory / "frames").mkdir(parents=True)
    (directory / "frames/f0.jpg").write_bytes(b"test-only-frame")
    source = {"id": "vid_local", "generation": 1, "metadata": '{"duration":8.0}'}
    frame = {
        "file": "f0.jpg",
        "frame_id": "vid_local_f0",
        "uri": "/media/vid_local/frames/f0.jpg",
        "t": 0.0,
    }
    frame2 = frame | {
        "file": "f1.jpg",
        "frame_id": "vid_local_f1",
        "t": 1.0,
        "uri": "/media/vid_local/frames/f1.jpg",
    }
    (directory / "frames/f1.jpg").write_bytes(b"test-only-frame")
    monkeypatch.setattr(local, "configured", lambda: (tmp_path / "python", model))
    monkeypatch.setattr(local, "source_digest", lambda directory: "a" * 64)
    monkeypatch.setattr(local, "frames", lambda *a, **kw: [frame, frame2])
    calls = []

    class Client:
        identity = (tmp_path / "python", model)
        process = type("Process", (), {"poll": lambda self: None})()

        def infer(self, prompt, images=(), max_tokens=512, schema=None):
            calls.append(bool(images))
            if "caption" in schema.get("properties", {}):
                return {
                    "text": json.dumps(
                        {"answer": "explicit test-only result", "caption": "red box"}
                    ),
                    "elapsed": 0.01,
                }
            result = (
                {
                    "summary": "explicit test transport",
                    "observations": [
                        {"description": "red box", "frames": [0, 1], "uncertain": False}
                    ],
                    "uncertainties": [],
                }
                if images
                else {"matches": [{"segment": 0, "observations": [0]}]}
            )
            return {"text": json.dumps(result), "elapsed": 0.01}

    monkeypatch.setattr(local, "_CLIENT", Client())
    answer = local.answer_source(tmp_path, source, "where?", None, lambda _: None, lambda: True)
    assert answer["status"] == "partial"
    assert answer["events"][0]["evidence"][0]["t"] == 0
    assert len(answer["events"][0]["evidence"]) == 2, "all matching observation frames are expanded"
    assert calls == [True, False, False]
    same = local.answer_source(tmp_path, source, "where?", None, lambda _: None, lambda: True)
    assert same == answer and calls == [True, False, False], (
        "completed synthesis is not dispatched again"
    )
    again = local.answer_source(
        tmp_path, source, "another question", None, lambda _: None, lambda: True
    )
    assert again["events"] and calls == [True, False, False, False, False], (
        "completed visual work is reused"
    )
    # A compact decoder grammar permits the union of frame IDs. Publication
    # must still reject a frame missing from the cited segment.
    original_infer = local._CLIENT.infer

    def invalid_reference(*args, **kwargs):
        result = original_infer(*args, **kwargs)
        payload = json.loads(result["text"])
        if "matches" not in payload:
            return result
        payload["matches"][0]["observations"] = [1]
        return result | {"text": json.dumps(payload)}

    local._CLIENT.infer = invalid_reference
    with pytest.raises(ValueError, match="local_answer_reference_invalid"):
        local.answer_source(
            tmp_path, source, "invalid reference", None, lambda _: None, lambda: True
        )

    def mixed_references(*args, **kwargs):
        result = original_infer(*args, **kwargs)
        payload = json.loads(result["text"])
        if "matches" not in payload:
            return result
        payload["matches"].append({"segment": 0, "observations": [7]})
        return result | {"text": json.dumps(payload)}

    local._CLIENT.infer = mixed_references
    partial = local.answer_source(
        tmp_path, source, "mixed references", None, lambda _: None, lambda: True
    )
    assert len(partial["events"]) == 1 and partial["status"] == "partial"
    assert "已排除1条" in partial["unresolved"][1]

    local._CLIENT.infer = lambda *a, **k: {"text": '{"matches": []}'}
    missing = local.answer_source(
        tmp_path, source, "missing target", None, lambda _: None, lambda: True
    )
    assert missing["status"] == "no_result" and missing["events"] == []
    assert "不能证明原视频中绝对不存在" in missing["answer"]

    def failed_editor(*args, **kwargs):
        if "caption" in kwargs["schema"]["properties"]:
            raise ValueError("test editor failure")
        return original_infer(*args, **kwargs)

    local._CLIENT.infer = failed_editor
    retained = local.answer_source(
        tmp_path, source, "editor failure", None, lambda _: None, lambda: True
    )
    assert retained["status"] == "partial" and retained["events"]
    assert any("test editor failure" in s for s in retained["unresolved"])


def test_local_parser_does_not_expose_reasoning_or_invent_missing_json():
    from frameseek.local_vision import json_response

    assert json_response('<think>private reasoning</think><answer>{"answer":"final"}</answer>') == {
        "answer": "final"
    }
    with pytest.raises(ValueError):
        json_response("no structured final answer")


@pytest.mark.anyio
async def test_known_local_failure_is_durable_and_never_falls_back_to_paid(tmp_path, monkeypatch):
    import frameseek.local_routes as local

    app, client, video, desk, session, index = await setup_index_page(tmp_path, monkeypatch)
    monkeypatch.setattr(local, "configured", lambda: ("test", "test"))
    calls = []

    def fail(*args):
        calls.append(1)
        raise ValueError("explicit test failure")

    monkeypatch.setattr(local, "answer_source", fail)
    body = {
        "investigation_id": desk["id"],
        "request_id": "local-error",
        "generation": 0,
        "question": "test failure only",
    }
    try:
        await client.post("/api/local-chat", json=body)
        for _ in range(100):
            result = (await client.post("/api/local-chat", json=body)).json()
            if result.get("response"):
                break
            await asyncio.sleep(0.01)
        assert result["response"]["status"] == "error"
        assert result["response"]["errors"][0]["code"] == "local_vision_failed"
        assert len(calls) == 1
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        await client.aclose()


def test_schema_reaches_the_private_worker_protocol():
    import io
    import json

    from frameseek.local_vision import LocalClient, packet_schema

    client = LocalClient.__new__(LocalClient)
    client.process = type("Process", (), {"stdin": io.StringIO()})()
    client.read = lambda timeout: {"text": "test-only"}
    schema = packet_schema(1)
    client.infer("test", schema=schema)
    sent = json.loads(client.process.stdin.getvalue())
    assert sent["schema"] == schema
    assert sent["schema"]["properties"]["observations"]["items"]["properties"]["frames"]["items"][
        "enum"
    ] == [0]


def test_selection_schema_uses_owned_observation_enums_without_generated_times():
    from frameseek.local_vision import selection_schema

    schema = selection_schema(
        [
            {"window": 0, "analysis": {"observations": [{}]}},
            {"window": 1, "analysis": {"observations": [{}, {}]}},
        ]
    )
    match = schema["properties"]["matches"]["items"]
    assert match["properties"]["segment"]["enum"] == [0, 1]
    assert match["properties"]["observations"]["items"]["enum"] == [0, 1]
    assert "frames" not in match["properties"] and "summary" not in match["properties"]


def test_answer_editor_keeps_appearance_questions_separate_from_counting(tmp_path):
    import copy
    import json

    from frameseek.local_vision import edit_grounded_answer

    prompts = []

    class Editor:
        def infer(self, prompt, **kwargs):
            prompts.append(prompt)
            return {"text": json.dumps({"answer": "test-only", "caption": "test caption"})}

    events = [{"summary": "a person wearing a red hat", "evidence": [{"caption": "old"}]}]
    appearance_answer = edit_grounded_answer(
        Editor(), tmp_path, "test-id", "找出戴红色帽子的人出现的时间", copy.deepcopy(events)
    )
    assert "1组画面" in appearance_answer and "1张引用" in appearance_answer
    assert "test-only" not in appearance_answer, (
        "appearance text uses actual matches, not free-form claims"
    )
    edit_grounded_answer(
        Editor(), tmp_path, "test-id", "这个人总共出现多少次", copy.deepcopy(events)
    )
    assert "不要将出现时间或存在性问题" in prompts[0]
    assert "无法确定用户要求的精确总数" not in prompts[0]
    assert "无法确定用户要求的精确总数" in prompts[1]
    assert "视频时间或片段编号" in prompts[0]


def test_query_specific_pixels_fill_caption_holes_and_reuse_receipts(tmp_path):
    import copy
    import json

    from frameseek.local_vision import refine_uncovered_frames

    frames = [
        {"frame_id": "f0", "file": "f0.jpg", "t": 0},
        {"frame_id": "f1", "file": "f1.jpg", "t": 1},
    ]
    rows = [{"window": 0, "frames": frames}]
    initial = {
        "claims": [{"segment": 0, "frames": [0], "summary": "test visible target"}],
        "uncertainties": [],
    }
    calls = []

    class Pixels:
        def infer(self, prompt, images, **kwargs):
            calls.append(images)
            return {"text": json.dumps({"matching_frames": [0]})}

    def run(question, client):
        return refine_uncovered_frames(
            client,
            tmp_path,
            "test-source-config",
            tmp_path,
            question,
            None,
            rows,
            copy.deepcopy(initial),
            lambda _: None,
            lambda: True,
        )

    result = run("target appearance times", Pixels())
    assert calls == [[tmp_path / "frames/f1.jpg"]], "a caption omission is checked in actual pixels"
    assert result["claims"][0]["frames"] == [0, 1]
    assert run("target appearance times", Pixels()) == result and len(calls) == 1

    class MissedBatch:
        def infer(self, *args, **kwargs):
            return {
                "text": json.dumps(
                    {"visible": "test-only visible target", "match": True}
                    if "match" in kwargs["schema"]["properties"]
                    else {"matching_frames": []}
                )
            }

    confirmed = run("batch missed target appearance times", MissedBatch())
    assert confirmed["claims"][0]["frames"] == [0, 1], (
        "a batch omission receives a single-image check"
    )

    class Unknown:
        def infer(self, *args, **kwargs):
            calls.append("unknown")
            raise ValueError("test_unknown_timeout")

    partial = run("another target appearance times", Unknown())
    assert partial["claims"][0]["frames"] == [0] and partial["uncertainties"]
    again = run("another target appearance times", Unknown())
    assert again["claims"][0]["frames"] == [0]
    assert calls.count("unknown") == 1, "unknown local dispatch is never silently resent"


def test_long_local_ranges_do_not_change_paid_default_window_bounds():
    from frameseek.agent import explicit_time_window

    question = "请检查01:00至04:00出现的画面"
    assert explicit_time_window(question, 400) is None
    assert explicit_time_window(question, 400, max_seconds=400) == {"start": 60, "end": 240}


def test_hour_long_index_grouping_preserves_every_timestamp_and_frame_reference():
    from frameseek.local_vision import group_index_windows

    events = [
        {
            "start": n * 8,
            "end": n * 8 + 7.001,
            "end_hms": str(n),
            "summary": "test-only",
            "evidence": [{"frame_id": f"f{n}_{i}", "t": n * 8 + i} for i in range(8)],
        }
        for n in range(450)
    ]
    grouped = group_index_windows(events)
    assert len(grouped) <= 100
    assert all(len(e["evidence"]) <= 128 for e in grouped)
    assert [f["t"] for e in grouped for f in e["evidence"]] == list(range(3600))
    assert grouped[-1]["end"] == events[-1]["end"]


def test_free_pixel_search_understands_presence_and_general_target_requests():
    from frameseek.local_vision import appearance_request

    assert appearance_request("视频里面有没有戴红色帽子、穿白色衣服的男人？")
    assert appearance_request("找出红箱子")
    assert appearance_request("Are there people wearing blue?")
    assert not appearance_request("这个人出现多少次？")


def test_explicit_free_cancellation_stops_worker_and_only_clears_its_new_markers(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace

    import frameseek.local_vision as local

    stopped = []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append(True))
    client = SimpleNamespace(process=process)
    monkeypatch.setattr(local, "_CLIENT", client)
    local.cancel_worker()
    assert stopped == [True] and client.cancelled is True
    starting = SimpleNamespace(process=process)
    monkeypatch.setattr(local, "_STARTING_CLIENT", starting)
    local.cancel_worker()
    assert starting.cancelled is True and stopped == [True, True]
    old = tmp_path / "old.started"
    current = tmp_path / "current.started"
    receipt = tmp_path / "current.raw.json"
    old.write_text(json.dumps({"started": 10}))
    current.write_text(json.dumps({"started": 30}))
    receipt.write_text('{"text":"saved output"}')
    local.clear_cancelled_markers(tmp_path, 20)
    assert old.exists() and receipt.exists() and not current.exists()
