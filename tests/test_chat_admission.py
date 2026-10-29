import pytest
from test_agent import setup_real
from test_tokenfactory import configuration, handler


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_continue_only_is_clarified_and_budget_preflight_never_creates_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    app, client, _, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=handler(calls)
    )
    try:
        before = len(calls)
        body = dict(
            investigation_id=desk["id"],
            request_id="admission_only",
            generation=0,
            question="继续这个问题",
        )
        assert (await client.post("/api/chat/prepare", json=body)).json()[
            "kind"
        ] == "clarification_required"
        with app.state.agent.ledger._connect() as conn:
            conn.execute("UPDATE budgets SET call_cap=used_calls+3")
        body["question"] = "谁投的三分最多？"
        result = (await client.post("/api/chat/prepare", json=body)).json()
        assert result["kind"] == "capability_blocked"
        assert "尚未完成全片" in result["message"]
        for question in ("整个比赛 多少个3分球投入？", "视频中箱子被搬动了多少次？"):
            body["question"] = question
            assert (await client.post("/api/chat/prepare", json=body)).json()[
                "kind"
            ] == "capability_blocked"
        body["question"] = "描述视频中的篮球场。"
        result = (await client.post("/api/chat/prepare", json=body)).json()
        assert result["kind"] == "budget_blocked"
        assert "最多剩3次" in result["message"]
        assert "尚未调用模型" in result["message"]
        body["question"] = "请查看视频00:05至00:11，描述箱子的位置。"
        assert (await client.post("/api/chat/prepare", json=body)).json()["kind"] == "ready"
        assert len(calls) == before
        with app.state.workspace.db() as conn:
            assert conn.execute("SELECT count(*) FROM queries").fetchone()[0] == 0
        app.state.agent.ledger.allow_repairs = False
        with app.state.agent.ledger._connect() as conn:
            conn.execute("UPDATE budgets SET call_cap=used_calls+2")
        assert (await client.post("/api/chat/prepare", json=body)).json()["kind"] == "ready"
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_invalid_time_windows_are_actionable_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    app, client, _, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=handler(calls)
    )
    try:
        before = len(calls)
        for window in (
            "99:00至99:05",
            "00:08至00:05",
            "00:05至00:99",
            "00:01至00:02和00:03至00:04",
        ):
            result = await client.post(
                "/api/chat/prepare",
                json={
                    "investigation_id": desk["id"],
                    "request_id": "invalid_window",
                    "generation": 0,
                    "question": f"请查看视频{window}，描述箱子的位置。",
                },
            )
            assert result.status_code == 200
            assert result.json()["kind"] == "clarification_required"
            assert "视频时间段无效" in result.json()["message"]
        assert len(calls) == before
        with app.state.workspace.db() as conn:
            assert conn.execute("SELECT count(*) FROM queries").fetchone()[0] == 0
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_ledger_outage_reports_unknown_capacity_without_dispatch(tmp_path, monkeypatch):
    from frameseek.attempts import BudgetDenied

    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    app, client, _, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=handler(calls)
    )
    try:
        before = len(calls)

        def unavailable(*args, **kwargs):
            raise BudgetDenied("Shared ledger unavailable; no automatic retry")

        monkeypatch.setattr(app.state.agent.ledger, "admit_cost", unavailable)
        monkeypatch.setattr(app.state.agent.ledger, "_connect", unavailable)
        result = await client.post(
            "/api/chat/prepare",
            json={
                "investigation_id": desk["id"],
                "request_id": "outage",
                "generation": 0,
                "question": "请查看视频00:05至00:08，描述箱子的位置。",
            },
        )
        assert result.status_code == 200
        assert result.json()["kind"] == "budget_unavailable"
        assert "不会自动重试" in result.json()["message"]
        status = await client.get("/api/runtime/status")
        assert status.status_code == 200
        assert status.json()["budget_status"] == "unavailable"
        assert not status.json()["dispatch_prerequisites_met"]
        assert "budget" not in status.json(), (
            "unknown capacity must not become a zero/fresh balance"
        )
        assert len(calls) == before
        with app.state.workspace.db() as conn:
            assert conn.execute("SELECT count(*) FROM queries").fetchone()[0] == 0
    finally:
        await client.aclose()
