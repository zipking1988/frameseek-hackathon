"""Transport mocks test integration; they are never presented as live model evaluation."""

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from frameseek.agent import (
    AgentError,
    Endpoint,
    Provider,
    Runtime,
    checked_embeddings,
    progress_answer,
    query_artifact_key,
)
from frameseek.api import create_app
from frameseek.attempts import ApprovedBudget, AttemptLedger, BudgetDenied
from frameseek.config import Settings
from frameseek.schema import Answer


@pytest.fixture
def anyio_backend():
    return "asyncio"


def configuration():
    def endpoint(role):
        return Endpoint(
            base_url="http://127.0.0.1:9000/v1",
            model="nvidia/" + role,
            revision="mock-contract-test",
            max_cost_micro=1,
        )

    return Runtime(
        vision=endpoint("vision"),
        embed=endpoint("embed"),
        rerank=endpoint("rerank"),
        text=endpoint("text"),
        budget_version="test_v1",
        calibration_reference="SYNTHETIC TEST ONLY",
        resource_cutoff_reference="MOCK",
        valid_until=time.time() + 3600,
        attempt_seconds=2,
        self_hosted_hosts=["127.0.0.1"],
    )


def approve(ledger, owner, calls=100, money=100):
    for scope in ("*", owner):
        ledger.install_approved_record(
            ApprovedBudget(
                scope, "test_v1", "TEST ONLY NO MONEY", min(50, calls), calls, money, "USD_MICRO"
            )
        )


def handler(calls, fail_zoom=False):
    def respond(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))
                    ]
                },
            )
        if request.url.path.endswith("/rerank"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": i, "relevance_score": 1.0}
                        for i in range(min(payload["top_n"], len(payload["documents"])))
                    ]
                },
            )
        content = payload["messages"][1]["content"]
        if isinstance(content, list):
            selected = [json.loads(item["text"]) for item in content[1:] if item["type"] == "text"]
            if content[0]["text"].startswith("Describe"):
                result = {
                    "frames": [
                        {"frame_id": f["frame_id"], "caption": "纸箱在工作台上"} for f in selected
                    ]
                }
            else:
                if fail_zoom:
                    raise httpx.ReadTimeout("mock transport timeout")
                result = {
                    "observations": [
                        {
                            "frame_ids": [selected[0]["frame_id"]],
                            "statement": "纸箱出现在工作台上。",
                        }
                    ],
                    "unresolved": [],
                }
        else:
            prompt = json.loads(content)
            if "candidates" in prompt:
                result = {"inspect": list(range(min(3, len(prompt["candidates"]))))}
            else:
                result = {
                    "answer": "可以在引用画面中看到纸箱。",
                    "followups": ["之后的位置呢？"],
                    "unresolved": [],
                }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    return respond


async def setup_real(
    tmp_path,
    monkeypatch,
    calls,
    fail_zoom=False,
    *,
    runtime_factory=configuration,
    response_handler=None,
):
    import frameseek.api

    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(frameseek.api, "load_runtime", runtime_factory)
    app = create_app(Settings(mode="real"))
    app.state.agent.transport = httpx.MockTransport(response_handler or handler(calls, fail_zoom))
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    session = (await client.get("/api/session")).json()
    client.headers["x-fs-csrf"] = session["csrf"]
    approve(app.state.agent.ledger, session["owner_id"])
    upload = await client.post(
        "/api/videos",
        content=Path("src/frameseek/static/demo/parcel.mp4").read_bytes(),
        headers={"content-type": "video/mp4"},
    )
    video = upload.json()["id"]
    for _ in range(100):
        detail = (await client.get("/api/videos/" + video)).json()
        if detail["job"]["state"] not in {"queued", "probing"}:
            break
        await asyncio.sleep(0.03)
    assert detail["job"]["state"] == "awaiting_configuration", detail
    assert (await client.post(f"/api/videos/{video}/analyze")).status_code == 202
    for _ in range(150):
        detail = (await client.get("/api/videos/" + video)).json()
        if detail["job"]["state"] in {"ready", "failed"}:
            break
        await asyncio.sleep(0.03)
    assert detail["job"]["state"] == "ready", detail
    desk = (await client.post("/api/investigations", json={"video_id": video})).json()
    return app, client, video, desk


@pytest.mark.anyio
async def test_analyze_waits_for_preflight_worker_to_release_terminal_slot(tmp_path, monkeypatch):
    import frameseek.api
    import frameseek.routes

    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(frameseek.api, "load_runtime", configuration)
    monkeypatch.setattr(frameseek.routes, "probe", lambda _directory: {})
    monkeypatch.setattr(
        frameseek.routes,
        "scene_windows",
        lambda *_args: {"duration": 1, "windows": [{"start": 0, "end": 1}]},
    )
    monkeypatch.setattr(frameseek.routes, "frames", lambda *_args: [])
    app = create_app(Settings(mode="real"))
    terminal_written, release, admission_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_stage = app.state.workspace.stage

    def slow_stage(video, owner, generation, state, **kwargs):
        original_stage(video, owner, generation, state, **kwargs)
        if state == "awaiting_configuration":
            terminal_written.set()
            release.wait(5)

    monkeypatch.setattr(app.state.workspace, "stage", slow_stage)
    monkeypatch.setattr(app.state.agent, "admit_ingest", lambda *_args: admission_started.set())
    monkeypatch.setattr(app.state.agent, "ingest", lambda *_args, **_kwargs: None)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.headers["x-fs-csrf"] = (await client.get("/api/session")).json()["csrf"]
    try:
        uploaded = await client.post(
            "/api/videos", content=b"synthetic", headers={"content-type": "video/mp4"}
        )
        video = uploaded.json()["id"]
        assert await asyncio.to_thread(terminal_written.wait, 2)
        assert (await client.get(f"/api/videos/{video}")).json()["job"]["state"] == (
            "awaiting_configuration"
        )
        analyze = asyncio.create_task(client.post(f"/api/videos/{video}/analyze"))
        assert await asyncio.to_thread(admission_started.wait, 2)
        await asyncio.sleep(0.1)
        assert not analyze.done()
        release.set()
        assert (await analyze).status_code == 202
    finally:
        release.set()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("request_id", ["req_first", "r" * 128])
async def test_upload_index_query_multi_turn_same_id_and_source_ownership(
    tmp_path, monkeypatch, request_id
):
    calls = []
    app, client, video, desk = await setup_real(tmp_path, monkeypatch, calls)
    try:
        assert (await client.get("/api/session")).json()["real_ready"]
        body = {
            "investigation_id": desk["id"],
            "request_id": request_id,
            "generation": 0,
            "question": "纸箱在哪？",
        }
        assert (await client.post("/api/chat/prepare", json=body)).json()["kind"] == "ready"
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        answer = Answer.model_validate(result["response"])
        assert answer.events and all(e.video_id == video for e in answer.events)
        assert all(
            [
                (await client.get(frame.uri)).status_code == 200
                for event in answer.events
                for frame in event.evidence
            ]
        )
        before = len(calls)
        repeated = await client.post("/api/chat", json=body)
        assert repeated.json() == result and len(calls) == before
        changed = await client.post("/api/chat", json={**body, "question": "别的问题"})
        assert changed.status_code == 409 and len(calls) == before
        follow = {
            **body,
            "request_id": "req_follow",
            "question": "它后来呢？",
            "parent_request_id": request_id,
        }
        response = await client.post("/api/chat", json=follow)
        assert response.status_code == 200 and response.json()["usedContext"]
        assert (
            await client.post(
                "/api/chat",
                json={**follow, "request_id": "req_forged", "parent_request_id": "someone_else"},
            )
        ).status_code == 409
        records = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()["items"]
        assert len(records) == 2 and all(r["state"] == "done" for r in records)
        # Media reaches only the self-hosted vision role, text gets no image URLs.
        for _, payload in calls:
            if payload.get("model") == "nvidia/text":
                assert isinstance(payload["messages"][1]["content"], str)
                assert "base64" not in payload["messages"][1]["content"]
        with app.state.agent.ledger._connect() as c:
            attempts = c.execute("SELECT kind,state FROM attempts").fetchall()
            assert all(r["state"] == "completed" for r in attempts)
            assert sum(r["kind"] == "zoom" for r in attempts) <= 6
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_unknown_same_request_never_resends_or_refunds(tmp_path, monkeypatch):
    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls, True)
    try:
        body = {
            "investigation_id": desk["id"],
            "request_id": "req_timeout",
            "generation": 0,
            "question": "纸箱在哪？",
        }
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 409 and response.json()["state"] == "unknown"
        before = len(calls)
        assert (await client.post("/api/chat", json=body)).json()["state"] == "unknown"
        assert len(calls) == before
        with app.state.agent.ledger._connect() as c:
            zoom = c.execute("SELECT * FROM attempts WHERE kind='zoom'").fetchone()
            assert zoom["state"] == "unknown" and zoom["money"] == 1
        data = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()["items"][0]
        assert data["input"] == {**body, "parent_request_id": None}
        assert data["state"] == "unknown"
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_pending_duplicate_and_reset_discard_late_answer(tmp_path, monkeypatch):
    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls)
    started, release = threading.Event(), threading.Event()
    real_query = app.state.agent.query

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real_query(*args, **kwargs)

    monkeypatch.setattr(app.state.agent, "query", slow)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_slow",
        "generation": 0,
        "question": "纸箱在哪？",
    }
    try:
        first = asyncio.create_task(client.post("/api/chat", json=body))
        await asyncio.to_thread(started.wait, 2)
        second = await client.post("/api/chat", json=body)
        assert second.status_code == 202 and second.json()["state"] == "pending"
        assert second.headers["Retry-After"] == "3"
        await client.put(
            f"/api/investigations/{desk['id']}", json={"revision": 0, "state": {"epoch": 1}}
        )
        release.set()
        assert (await first).status_code == 409
        assert (await client.post("/api/chat", json=body)).status_code == 409
    finally:
        release.set()
        await client.aclose()


@pytest.mark.anyio
async def test_cancel_stops_later_dispatch_and_preserves_terminal_result(tmp_path, monkeypatch):
    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls)
    started, release = threading.Event(), threading.Event()
    real_query = app.state.agent.query

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real_query(*args, **kwargs)

    monkeypatch.setattr(app.state.agent, "query", slow)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_cancel",
        "generation": 0,
        "question": "纸箱在哪？",
    }
    route = f"/api/investigations/{desk['id']}/requests/req_cancel/cancel"
    try:
        first = asyncio.create_task(client.post("/api/chat", json=body))
        assert await asyncio.to_thread(started.wait, 2)
        before = len(calls)
        cancelled = await client.post(route)
        assert cancelled.status_code == 200
        assert cancelled.json()["response"]["errors"][0]["code"] == "query_cancelled"
        release.set()
        assert (await first).json() == cancelled.json()
        assert (await client.post("/api/chat", json=body)).json() == cancelled.json()
        assert (await client.post(route)).json() == cancelled.json()
        assert len(calls) == before
    finally:
        release.set()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["matching", "other_investigation", "other_generation", "legacy"])
async def test_cancel_only_recovers_progress_for_the_same_query_identity(
    tmp_path, monkeypatch, case
):
    calls = []
    app, client, video, desk = await setup_real(tmp_path, monkeypatch, calls)
    ws = app.state.workspace
    session = (await client.get("/api/session")).json()
    owner = session["owner_id"]
    source = ws.owned("videos", video, owner)
    frame = json.loads(source["manifest"])["segments"][0]["frames"][0]
    evidence = progress_answer(
        video,
        [{"frame_ids": [frame["frame_id"]], "statement": "已验证画面", "start": 0, "end": 18}],
        {frame["frame_id"]: frame},
        1,
    )
    started, release = threading.Event(), threading.Event()
    write_progress = True

    def paused_query(*args, **kwargs):
        if write_progress:
            key = query_artifact_key(
                video,
                source["generation"],
                owner,
                kwargs["investigation_id"],
                args[3],
                kwargs["payload_hash"],
            )
            name = (
                args[3] + "_verified.json"
                if case == "legacy"
                else "query_" + key + "_verified.json"
            )
            (ws.root / video / name).write_text(json.dumps(evidence, ensure_ascii=False))
        started.set()
        release.wait(5)
        raise AgentError("query_cancelled")

    monkeypatch.setattr(app.state.agent, "query", paused_query)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_scoped_progress",
        "generation": 0,
        "question": "原问题",
    }
    tasks = []
    try:
        tasks.append(asyncio.create_task(client.post("/api/chat", json=body)))
        assert await asyncio.to_thread(started.wait, 2)
        if case == "other_investigation":
            assert (await client.delete(f"/api/investigations/{desk['id']}")).status_code == 204
            replacement = (
                await client.post("/api/investigations", json={"video_id": video})
            ).json()
            body = {**body, "investigation_id": replacement["id"], "question": "不同问题"}
            write_progress = False
            started.clear()
            tasks.append(asyncio.create_task(client.post("/api/chat", json=body)))
            assert await asyncio.to_thread(started.wait, 2)
        elif case == "other_generation":
            with ws.db() as c:
                c.execute("UPDATE videos SET generation=generation+1 WHERE id=?", (video,))
        result = await client.post(
            f"/api/investigations/{body['investigation_id']}/requests/{body['request_id']}/cancel"
        )
        assert result.status_code == 200
        if case == "matching":
            assert result.json()["response"] == evidence
            assert result.json()["source_generation"] == source["generation"]
        else:
            assert result.json()["response"]["errors"][0]["code"] == "query_cancelled"
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()


@pytest.mark.anyio
async def test_new_query_recovers_only_matching_unknown_progress(tmp_path, monkeypatch):
    calls = []
    app, client, video, desk = await setup_real(tmp_path, monkeypatch, calls)
    ws = app.state.workspace
    owner = (await client.get("/api/session")).json()["owner_id"]
    source = ws.owned("videos", video, owner)
    frame = json.loads(source["manifest"])["segments"][0]["frames"][0]
    evidence = progress_answer(
        video,
        [{"frame_ids": [frame["frame_id"]], "statement": "已验证画面", "start": 0, "end": 18}],
        {frame["frame_id"]: frame},
        1,
    )

    def fake_query(*args, **kwargs):
        if args[3] == "req_unknown_progress":
            key = query_artifact_key(
                video, source["generation"], owner, desk["id"], args[3], kwargs["payload_hash"]
            )
            (ws.root / video / ("query_" + key + "_verified.json")).write_text(
                json.dumps(evidence, ensure_ascii=False)
            )
            raise AgentError("model_unknown")
        return {
            "schema_version": "0.1",
            "status": "no_result",
            "timebase": "video_absolute_seconds",
            "answer": "没有新的证据",
            "events": [],
            "unresolved": [],
            "followups": [],
            "errors": [],
            "trace": {
                "visible": {"channels": [], "segments_reviewed": 0, "evidence_segments": 0},
                "debug": None,
            },
        }

    monkeypatch.setattr(app.state.agent, "query", fake_query)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_unknown_progress",
        "generation": 0,
        "question": "原问题",
    }
    try:
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 409 and response.json()["state"] == "unknown"
        assert (
            await client.post("/api/chat", json={**body, "request_id": "req_next"})
        ).status_code == 200
        records = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()["items"]
        old = next(row for row in records if row["id"] == body["request_id"])
        assert old["result"]["response"] == evidence
        assert old["result"]["source_generation"] == source["generation"]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_reused_request_id_does_not_overwrite_old_frames_or_replay_attempt(
    tmp_path, monkeypatch
):
    import frameseek.agent as agent_module

    calls = []
    app, client, video, desk = await setup_real(tmp_path, monkeypatch, calls)
    original_frames = agent_module.frames
    extracted = []

    def remember_frames(*args, **kwargs):
        result = original_frames(*args, **kwargs)
        extracted.append(result)
        return result

    monkeypatch.setattr(agent_module, "frames", remember_frames)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_reused_frames",
        "generation": 0,
        "question": "00:00-00:02 画面里有什么？",
    }
    try:
        first = await client.post("/api/chat", json=body)
        assert first.status_code == 200 and first.json()["response"]["events"]
        assert len(extracted) == 1 and extracted[0]
        old_files = {
            f["file"]: (app.state.workspace.root / video / "frames" / f["file"]).read_bytes()
            for f in extracted[0]
        }
        before = len(calls)
        assert (await client.delete(f"/api/investigations/{desk['id']}")).status_code == 204
        next_desk = (await client.post("/api/investigations", json={"video_id": video})).json()
        await client.post("/api/chat", json={**body, "investigation_id": next_desk["id"]})
        assert len(extracted) == 2 and extracted[1]
        assert set(old_files).isdisjoint(f["file"] for f in extracted[1])
        assert all(
            (app.state.workspace.root / video / "frames" / name).read_bytes() == data
            for name, data in old_files.items()
        )
        assert len(calls) == before, "existing attempt identity must not be dispatched again"
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_and_local_followups_reject_stale_source_generation(
    tmp_path, monkeypatch, legacy
):
    import frameseek.local_routes as local_routes

    calls = []
    app, client, video, desk = await setup_real(tmp_path, monkeypatch, calls)
    monkeypatch.setattr(local_routes, "configured", lambda: ("test-python", "test-model"))
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_context_generation",
        "generation": 0,
        "question": "纸箱在哪里？",
    }
    try:
        parent = (await client.post("/api/chat", json=body)).json()
        assert parent["response"]["events"] and parent["source_generation"] == 1
        if legacy:
            parent.pop("source_generation")
            with app.state.workspace.db() as c:
                c.execute(
                    "UPDATE queries SET result=? WHERE id=?",
                    (json.dumps(parent), body["request_id"]),
                )
        follow = {
            **body,
            "request_id": "req_context_followup",
            "question": "画面里是什么颜色？",
            "parent_request_id": body["request_id"],
        }
        for endpoint in ("/api/chat/prepare", "/api/local-chat/prepare"):
            result = await client.post(endpoint, json=follow)
            assert result.status_code == 200 and result.json()["kind"] == "ready", result.text
        before = len(calls)
        with app.state.workspace.db() as c:
            c.execute("UPDATE videos SET generation=generation+1 WHERE id=?", (video,))
        for endpoint in ("/api/chat/prepare", "/api/local-chat/prepare"):
            blocked = await client.post(endpoint, json=follow)
            assert (
                blocked.status_code == 409
                and blocked.json()["detail"] == "stale_context_generation"
            )
        history = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()["items"]
        assert (
            next(item for item in history if item["id"] == body["request_id"])["result"] == parent
        )
        assert len(calls) == before
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_query_first", [False, True])
@pytest.mark.parametrize("unknown_failure", [False, True])
async def test_query_result_after_source_generation_changes_is_not_published(
    tmp_path, monkeypatch, cancel_query_first, unknown_failure
):
    calls = []
    app, client, video, desk = await setup_real(tmp_path, monkeypatch, calls)
    started, release = threading.Event(), threading.Event()

    def late_query(*_args, **_kwargs):
        started.set()
        release.wait(5)
        if unknown_failure:
            raise AgentError("model_unknown")
        return {
            "schema_version": "0.1",
            "status": "no_result",
            "timebase": "video_absolute_seconds",
            "answer": "迟到的结果",
            "events": [],
            "unresolved": [],
            "followups": [],
            "errors": [],
            "trace": {
                "visible": {"channels": [], "segments_reviewed": 0, "evidence_segments": 0},
                "debug": None,
            },
        }

    monkeypatch.setattr(app.state.agent, "query", late_query)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_source_changed",
        "generation": 0,
        "question": "纸箱在哪里？",
    }
    try:
        pending = asyncio.create_task(client.post("/api/chat", json=body))
        assert await asyncio.to_thread(started.wait, 2)
        assert (await client.post(f"/api/videos/{video}/cancel")).status_code == 202
        cancelled = None
        if cancel_query_first:
            cancelled = await client.post(
                f"/api/investigations/{desk['id']}/requests/{body['request_id']}/cancel"
            )
            assert cancelled.status_code == 200
        release.set()
        response = await pending
        assert response.status_code == 409 and response.json()["detail"] == "stale_video_generation"
        records = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()["items"]
        row = next(item for item in records if item["id"] == body["request_id"])
        if cancel_query_first:
            assert row["state"] == "done" and row["result"] == cancelled.json()
        else:
            assert row["state"] == "unknown" and not row["result"]
    finally:
        release.set()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["unknown", "cancel", "reset", "delete"])
@pytest.mark.parametrize("failure", [RuntimeError, OSError])
async def test_unexpected_query_failure_is_terminal_without_overwriting_context(
    tmp_path, monkeypatch, outcome, failure
):
    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls)
    started, release = threading.Event(), threading.Event()

    def fail(*_args, **_kwargs):
        started.set()
        release.wait(5)
        raise failure("private exception details")

    monkeypatch.setattr(app.state.agent, "query", fail)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_unexpected",
        "generation": 0,
        "question": "纸箱在哪？",
    }
    try:
        first = asyncio.create_task(client.post("/api/chat", json=body))
        assert await asyncio.to_thread(started.wait, 2)
        before = len(calls)
        if outcome == "cancel":
            cancelled = await client.post(
                f"/api/investigations/{desk['id']}/requests/req_unexpected/cancel"
            )
            assert cancelled.status_code == 200
        elif outcome == "reset":
            reset = await client.put(
                f"/api/investigations/{desk['id']}",
                json={"revision": 0, "state": {"epoch": 1}},
            )
            assert reset.status_code == 200
        elif outcome == "delete":
            assert (await client.delete(f"/api/investigations/{desk['id']}")).status_code == 204
        release.set()
        response = await first
        if outcome == "unknown":
            assert response.status_code == 409 and response.json()["state"] == "unknown"
            assert (await client.post("/api/chat", json=body)).json()["state"] == "unknown"
        elif outcome == "cancel":
            assert response.status_code == 200 and response.json() == cancelled.json()
        else:
            assert response.status_code == (409 if outcome == "reset" else 410)
        assert "private exception details" not in response.text
        assert len(calls) == before
        assert app.state.agent.slots.acquire(blocking=False)
        assert app.state.agent.slots.acquire(blocking=False)
        app.state.agent.slots.release()
        app.state.agent.slots.release()
    finally:
        release.set()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["insert", "commit"])
async def test_chat_admission_database_failure_rolls_back_and_releases_slot(
    tmp_path, monkeypatch, failure
):
    import sqlite3
    from contextlib import contextmanager

    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls)
    ws = app.state.workspace
    owner = (await client.get("/api/session")).json()["owner_id"]
    original_db = ws.db
    with original_db() as c:
        c.execute(
            "INSERT INTO queries(id,investigation,owner,generation,hash,state,updated,input) "
            "VALUES ('previous_unknown',?,?,0,'old','unknown',?,?)",
            (desk["id"], owner, time.time(), json.dumps({"question": "之前的问题"})),
        )
        if failure == "insert":
            c.execute(
                "CREATE TRIGGER fail_query_admission BEFORE INSERT ON queries "
                "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
            )
        else:
            c.execute("CREATE TABLE admission_parent(id INTEGER PRIMARY KEY)")
            c.execute(
                "CREATE TABLE admission_child(id INTEGER REFERENCES admission_parent(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
            c.execute(
                "CREATE TRIGGER fail_query_admission AFTER INSERT ON queries "
                "BEGIN INSERT INTO admission_child VALUES (999); END"
            )

    @contextmanager
    def checked_db():
        with original_db() as c:
            c.execute("PRAGMA foreign_keys=ON")
            yield c

    monkeypatch.setattr(ws, "db", checked_db)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_admission_failure",
        "generation": 0,
        "question": "纸箱在哪？",
    }
    try:
        before = len(calls)
        with pytest.raises(sqlite3.IntegrityError):
            await client.post("/api/chat", json=body)
        assert len(calls) == before
        with original_db() as c:
            assert (
                c.execute(
                    "SELECT state FROM queries WHERE owner=? AND id='previous_unknown'", (owner,)
                ).fetchone()["state"]
                == "unknown"
            )
            assert (
                c.execute(
                    "SELECT 1 FROM queries WHERE owner=? AND id='req_admission_failure'", (owner,)
                ).fetchone()
                is None
            )
        assert app.state.agent.slots.acquire(blocking=False)
        assert app.state.agent.slots.acquire(blocking=False)
        app.state.agent.slots.release()
        app.state.agent.slots.release()
    finally:
        await client.aclose()


def test_unapproved_budget_and_expired_calibration_dispatch_nothing(tmp_path):
    runtime = configuration()
    ledger = AttemptLedger(tmp_path / "ledger.sqlite")
    calls = []
    provider = Provider(runtime, ledger, lambda: None, httpx.MockTransport(handler(calls)))
    kwargs = dict(
        owner="owner",
        attempt="a",
        kind="embed",
        version="test_v1",
        category="query",
        deadline=time.monotonic() + 10,
    )
    with pytest.raises(BudgetDenied):
        provider.call("embed", "/embeddings", {"model": "m", "input": ["x"]}, **kwargs)
    assert not calls
    approve(ledger, "owner", 1, 1)
    runtime.valid_until = 0
    with pytest.raises(AgentError, match="real_blocked"):
        provider.call("embed", "/embeddings", {"model": "m", "input": ["x"]}, **kwargs)
    assert not calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stage,malformed,expected_status",
    [
        ("envelope", {"choices": [None]}, "error"),
        ("zoom", {"observations": [None], "unresolved": []}, "error"),
        ("zoom", {"observations": [], "unresolved": None}, "error"),
        ("synthesis", {"answer": None, "followups": [], "unresolved": []}, "partial"),
        ("synthesis", {"answer": "answer", "followups": {}, "unresolved": []}, "partial"),
        ("rerank", {"results": [None]}, "error"),
        ("embed", {"data": [{"index": "0", "embedding": [1.0, 0.0]}]}, "error"),
    ],
)
async def test_malformed_model_output_is_terminal_and_keeps_verified_evidence(
    tmp_path, monkeypatch, stage, malformed, expected_status
):
    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls)
    normal = handler(calls)

    def respond(request):
        payload = json.loads(request.content)
        content = payload.get("messages", [{}, {}])[1].get("content")
        replace = (
            stage == "embed"
            and request.url.path.endswith("/embeddings")
            or stage == "rerank"
            and request.url.path.endswith("/rerank")
            or stage == "envelope"
            and isinstance(content, str)
            or stage == "zoom"
            and isinstance(content, list)
            or stage == "synthesis"
            and isinstance(content, str)
            and "observations" in json.loads(content)
        )
        if replace:
            result = (
                malformed
                if stage in {"embed", "rerank", "envelope"}
                else {"choices": [{"message": {"content": json.dumps(malformed)}}]}
            )
            return httpx.Response(200, json=result)
        return normal(request)

    app.state.agent.transport = httpx.MockTransport(respond)
    body = {
        "investigation_id": desk["id"],
        "request_id": "req_malformed",
        "generation": 0,
        "question": "纸箱在哪？",
    }
    try:
        first = await client.post("/api/chat", json=body)
        assert first.status_code == (200 if expected_status == "partial" else 502), first.text
        answer = Answer.model_validate(first.json()["response"])
        assert answer.status == expected_status
        assert bool(answer.events) == (expected_status == "partial")
        before = len(calls)
        assert (await client.post("/api/chat", json=body)).json() == first.json()
        assert len(calls) == before
        stored = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()["items"]
        assert stored[0]["state"] == "done"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "data,code",
    [
        ([{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}], "indices"),
        ([{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0, 3.0]}], "dimension"),
        ([{"index": 0, "embedding": [1.0]}, None], "indices"),
    ],
)
def test_embedding_response_requires_unique_complete_indices_and_dimensions(tmp_path, data, code):
    ledger = AttemptLedger(tmp_path / "ledger.sqlite")
    approve(ledger, "owner")
    provider = Provider(
        configuration(),
        ledger,
        lambda: None,
        httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})),
    )
    result = provider.call(
        "embed",
        "/embeddings",
        {"input": ["one", "two"]},
        owner="owner",
        attempt="embedding",
        kind="embed",
        version="test_v1",
        category="ingest",
        deadline=time.monotonic() + 10,
    )
    with pytest.raises(AgentError, match=code):
        checked_embeddings(result, 2)


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_runtime", [False, True])
async def test_exact_initial_budget_allows_cache_only_ingest_resume_and_checks_revocation(
    tmp_path, monkeypatch, legacy_runtime
):
    import frameseek.api

    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(frameseek.api, "load_runtime", configuration)
    app = create_app(Settings(mode="real"))
    calls = []
    app.state.agent.transport = httpx.MockTransport(handler(calls))
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    session = (await client.get("/api/session")).json()
    owner = session["owner_id"]
    client.headers["x-fs-csrf"] = session["csrf"]
    for scope in ("*", owner):
        app.state.agent.ledger.install_approved_record(
            ApprovedBudget(scope, "test_v1", "TEST ONLY", 1, 3, 3, "USD_MICRO")
        )
    real_publish = app.state.workspace.publish
    failures = []

    def fail_once(*args):
        if not failures:
            failures.append(True)
            raise OSError("synthetic publication failure")
        return real_publish(*args)

    monkeypatch.setattr(app.state.workspace, "publish", fail_once)
    try:
        upload = await client.post(
            "/api/videos",
            content=Path("src/frameseek/static/demo/parcel.mp4").read_bytes(),
            headers={"content-type": "video/mp4"},
        )
        video = upload.json()["id"]
        for _ in range(100):
            detail = (await client.get(f"/api/videos/{video}")).json()
            if detail["job"]["state"] not in {"queued", "probing"}:
                break
            await asyncio.sleep(0.03)
        assert (await client.post(f"/api/videos/{video}/analyze")).status_code == 202
        for _ in range(150):
            detail = (await client.get(f"/api/videos/{video}")).json()
            if detail["job"]["state"] == "failed":
                break
            await asyncio.sleep(0.03)
        assert detail["job"]["state"] == "failed"
        if legacy_runtime:
            generation = app.state.workspace.owned("videos", video, owner)["generation"]
            frozen = tmp_path / video / f"run-config-{generation}.json"
            saved = json.loads(frozen.read_text())
            saved["runtime"].pop("provider")
            saved["runtime"].pop("allow_google_media")
            frozen.write_text(json.dumps(saved))
        before = len(calls)
        app.state.agent.admit_ingest(video, owner)
        response = await client.post(f"/api/videos/{video}/analyze")
        assert response.status_code == 202, response.text
        for _ in range(100):
            detail = (await client.get(f"/api/videos/{video}")).json()
            if detail["job"]["state"] == "ready":
                break
            await asyncio.sleep(0.03)
        assert detail["job"]["state"] == "ready", detail
        assert len(calls) == before == 2
        app.state.agent.ledger.restrict(owner, "test_v1")
        with pytest.raises(BudgetDenied):
            app.state.agent.admit_ingest(video, owner)
        assert len(calls) == before
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_cancelled_analysis_can_start_new_generation_without_overwriting_old_artifacts(
    tmp_path, monkeypatch
):
    calls = []
    app, client, video, _ = await setup_real(tmp_path, monkeypatch, calls)
    owner = app.state.workspace.authenticate(client.cookies.get("fs_session"))["id"]
    directory = tmp_path / video
    original = json.loads((directory / "segments-1.json").read_text())
    original_frames = {
        frame["file"]: (directory / "frames" / frame["file"]).read_bytes()
        for segment in original
        for frame in segment["frames"]
    }
    try:
        app.state.agent.settings = Settings(mode="real", coarse_frames=8)
        with pytest.raises(AgentError, match="index_configuration_changed"):
            app.state.agent.admit_ingest(video, owner)
        assert (await client.post(f"/api/videos/{video}/cancel")).status_code == 202
        response = await client.post(f"/api/videos/{video}/analyze")
        assert response.status_code == 202, response.text
        for _ in range(150):
            detail = (await client.get(f"/api/videos/{video}")).json()
            if detail["job"]["state"] in {"ready", "failed"}:
                break
            await asyncio.sleep(0.03)
        assert detail["job"]["state"] == "ready", detail
        assert (directory / "segments-2.json").is_file()
        assert (directory / "run-config-2.json").is_file()
        assert json.loads((directory / "segments-1.json").read_text()) == original
        assert all(
            (directory / "frames" / name).read_bytes() == data
            for name, data in original_frames.items()
        )
        current = json.loads(app.state.workspace.owned("videos", video, owner)["manifest"])
        assert current["generation"] == 2
        assert all(
            "g2_" in frame["file"] for segment in current["segments"] for frame in segment["frames"]
        )
        assert (await client.post(f"/api/videos/{video}/cancel")).status_code == 202
        attempt = f"{video}_3_coarse_0"
        app.state.agent.ledger.reserve(
            attempt_id=attempt,
            owner=owner,
            version="test_v1",
            payload_hash="synthetic-timeout",
            kind="coarse",
            money_units=1,
            currency_unit="USD_MICRO",
        )
        app.state.agent.ledger.transition(attempt, "dispatched")
        app.state.agent.ledger.transition(attempt, "unknown")
        before = len(calls)
        response = await client.post(f"/api/videos/{video}/analyze")
        assert response.status_code == 409
        assert response.json()["detail"] == "unknown_attempt_do_not_resend"
        assert len(calls) == before
    finally:
        await client.aclose()
