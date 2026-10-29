import json

import httpx
import pytest
from test_agent import setup_real
from test_tokenfactory import configuration, handler

from frameseek.agent import referenced_frame_window


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["valid", "negative", "foreign_frame"])
async def test_one_remaining_call_short_window_has_no_text_rewrite_or_retry(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    working = handler(calls)
    querying = False

    def respond(request):
        if querying and mode != "valid":
            payload = json.loads(request.content)
            calls.append((str(request.url), payload))
            assert isinstance(payload["messages"][1]["content"], list)
            result = (
                {"observations": [], "no_match": True}
                if mode == "negative"
                else {"observations": [{"frame_ids": ["foreign"], "statement": "不能接受"}]}
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(result)}}
                    ]
                },
            )
        return working(request)

    app, client, _, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=respond
    )
    try:
        app.state.agent.ledger.allow_repairs = False
        app.state.agent.ledger.direct_observations = True
        with app.state.agent.ledger._connect() as conn:
            conn.execute("UPDATE budgets SET call_cap=used_calls+1, money_cap=used_money+1")
        before = len(calls)
        querying = True
        body = dict(
            investigation_id=desk["id"],
            request_id="one_visual_call",
            generation=0,
            question="请查看视频00:05至00:08，描述纸箱的位置。",
        )
        assert (await client.post("/api/chat/prepare", json=body)).json()["kind"] == "ready"
        result = (await client.post("/api/chat", json=body)).json()
        response = result["response"]
        assert len(calls) == before + 1
        if mode == "valid":
            assert response["status"] == "partial"
            assert response["events"]
            for event in response["events"]:
                for frame in event["evidence"]:
                    assert 5 <= frame["t"] < 8
                    assert (await client.get(frame["uri"])).status_code == 200
        elif mode == "negative":
            assert response["status"] == "no_result"
            assert response["events"] == []
            assert "抽样画面" in response["answer"]
            assert "不能据此断言整段视频" in response["answer"]
        else:
            assert response["status"] == "error"
            assert response["events"] == []
        assert (await client.post("/api/chat", json=body)).json() == result
        assert len(calls) == before + 1
        with app.state.agent.ledger._connect() as conn:
            assert all(
                row[0] == 0 for row in conn.execute("SELECT call_cap-used_calls FROM budgets")
            )
        status = (await client.get("/api/runtime/status")).json()
        assert status["budget"]["remaining_calls"] == 0
        assert status["budget"]["short_window_calls"] == 1
    finally:
        await client.aclose()


def test_context_uses_only_unambiguous_single_frame_attribute_questions():
    context = {"events": [{"evidence": [{"t": 326.1}]}]}
    assert referenced_frame_window("这个画面里比赛还剩多少时间？", context, 384) == {
        "start": 326.1,
        "end": 327.1,
    }
    for question in ("继续这个问题", "这个画面前后发生什么？", "这个画面谁投的三分最多？"):
        assert referenced_frame_window(question, context, 384) is None
    context["events"][0]["evidence"].append({"t": 327.1})
    assert referenced_frame_window("这个画面里比赛还剩多少时间？", context, 384) is None
    for invalid in (float("nan"), float("inf"), True, -1, 384, [], "326"):
        context["events"][0]["evidence"] = [{"t": invalid}]
        assert referenced_frame_window("这个画面里比赛还剩多少时间？", context, 384) is None
