"""Transport mocks test integration; they are never presented as live model evaluation."""

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from frameseek.agent import AgentError, Endpoint, Provider, Runtime, checked_embeddings
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
