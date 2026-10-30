import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from frameseek.api import create_app
from frameseek.config import Settings
from frameseek.workspace import Workspace


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))
    return create_app(Settings())


async def client_for(app):
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    session = (await client.get("/api/session")).json()
    client.headers["x-fs-csrf"] = session["csrf"]
    return client


@pytest.mark.anyio
async def test_refresh_durable_cas_and_owner_isolation(app):
    a, b = await client_for(app), await client_for(app)
    try:
        created = (await a.post("/api/investigations", json={})).json()
        state = {
            "title": "位置调查",
            "epoch": 2,
            "note": "对照证据",
            "records": [],
            "pins": [],
            "canvas": {"view": {"x": 84, "y": -120, "scale": 0.7}, "nodes": []},
        }
        route = "/api/investigations/" + created["id"]
        assert (await a.put(route, json={"revision": 0, "state": state})).status_code == 200
        assert (await a.put(route, json={"revision": 0, "state": state})).status_code == 409
        assert (await b.get(route)).status_code == 404
        assert (await b.get("/api/investigations")).json()["items"] == []
        # A fresh DB wrapper simulates process restart, with the same owner cookie.
        restored = Workspace(app.state.workspace.root).get(
            created["id"], app.state.workspace.authenticate(a.cookies.get("fs_session"))["id"]
        )
        assert restored["state"] == state and restored["revision"] == 1
        assert (await a.delete(route)).status_code == 204
        assert (await a.get(route)).status_code == 410
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.anyio
async def test_csrf_origin_expired_session_and_request_boundary(app):
    a = await client_for(app)
    try:
        response = await a.post("/api/investigations", json={}, headers={"origin": "http://evil"})
        assert response.status_code == 403
        token = a.headers.pop("x-fs-csrf")
        assert (await a.post("/api/investigations", json={})).status_code == 403
        a.headers["x-fs-csrf"] = token
        assert (await a.post("/api/investigations", json={"budget_cap": 1000})).status_code == 422
        desk = (await a.post("/api/investigations", json={})).json()
        for state in ({"records": None}, {"pins": {}}, {"records": 1}):
            assert (
                await a.put(
                    "/api/investigations/" + desk["id"],
                    json={"revision": 0, "state": state},
                )
            ).status_code == 422
        with app.state.workspace.db() as c:
            c.execute("UPDATE sessions SET expires=0")
        assert (await a.get("/api/videos")).status_code == 401
    finally:
        await a.aclose()


@pytest.mark.anyio
async def test_delete_or_cancel_before_worker_start_releases_capacity(app, monkeypatch):
    import types

    import frameseek.routes

    pending = []

    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(frameseek.routes, "threading", types.SimpleNamespace(Thread=DeferredThread))
    a = await client_for(app)
    try:
        for _ in range(2):
            uploaded = await a.post(
                "/api/videos", content=b"deferred", headers={"content-type": "video/mp4"}
            )
            assert uploaded.status_code == 201
            assert (await a.delete("/api/videos/" + uploaded.json()["id"])).status_code == 204
            pending.pop().run()
        uploaded = await a.post(
            "/api/videos", content=b"deferred", headers={"content-type": "video/mp4"}
        )
        assert uploaded.status_code == 201
        video = uploaded.json()["id"]
        original = pending.pop()
        cancelled = await a.post(f"/api/videos/{video}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["generation"] == 2
        original.run()
        assert (await a.get(f"/api/videos/{video}")).json()["job"]["state"] == "cancelled"
        assert (await a.post(f"/api/videos/{video}/retry")).status_code == 202
        assert pending[-1].args[2] == 2
    finally:
        await a.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["probe", "analysis", "cancel", "delete"])
async def test_unexpected_worker_failure_is_terminal_and_preserves_invalidation(
    app, monkeypatch, outcome
):
    import types

    import frameseek.routes

    pending = []

    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

    monkeypatch.setattr(frameseek.routes, "threading", types.SimpleNamespace(Thread=DeferredThread))
    a = await client_for(app)
    try:
        uploaded = await a.post(
            "/api/videos", content=b"deferred", headers={"content-type": "video/mp4"}
        )
        video = uploaded.json()["id"]
        worker = pending.pop()
        ws = app.state.workspace
        owner = ws.authenticate(a.cookies.get("fs_session"))["id"]

        def fail(*args, **kwargs):
            if outcome == "cancel":
                with ws.db() as c:
                    c.execute("UPDATE videos SET generation=generation+1 WHERE id=?", (video,))
                    c.execute(
                        "UPDATE jobs SET state='cancelled',generation=2,lease=NULL WHERE video=?",
                        (video,),
                    )
            elif outcome == "delete":
                ws.delete("videos", video, owner)
            raise RuntimeError("unexpected worker failure")

        if outcome == "analysis":
            monkeypatch.setattr(app.state.agent, "ingest", fail)
            worker.target(*worker.args[:3], True)
        else:
            monkeypatch.setattr(frameseek.routes, "probe", fail)
            worker.target(*worker.args)
        with ws.db() as c:
            job = c.execute("SELECT * FROM jobs WHERE video=?", (video,)).fetchone()
        if outcome in {"cancel", "delete"}:
            assert job["state"] == "cancelled"
        else:
            assert job["state"] == "failed" and job["lease"] is None
            assert job["error"] == ("unknown" if outcome == "analysis" else "analysis_failed")
    finally:
        await a.aclose()


@pytest.mark.anyio
async def test_worker_start_failure_releases_capacity_and_lease(app, monkeypatch):
    import types

    import frameseek.routes

    pending = []
    fail_start = False

    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            if fail_start:
                raise RuntimeError("thread unavailable")
            pending.append(self)

    monkeypatch.setattr(frameseek.routes, "threading", types.SimpleNamespace(Thread=DeferredThread))
    a = await client_for(app)
    try:
        uploaded = await a.post(
            "/api/videos", content=b"deferred", headers={"content-type": "video/mp4"}
        )
        video = uploaded.json()["id"]

        def fail_probe(*_):
            raise ValueError("probe failed")

        monkeypatch.setattr(frameseek.routes, "probe", fail_probe)
        worker = pending.pop()
        worker.target(*worker.args)
        fail_start = True
        response = await a.post(f"/api/videos/{video}/retry")
        assert response.status_code == 503
        job = (await a.get(f"/api/videos/{video}")).json()["job"]
        assert job["state"] == "failed" and job["lease"] is None
        assert job["error"] == "worker_start_failed"
        fail_start = False
        assert (await a.post(f"/api/videos/{video}/retry")).status_code == 202
    finally:
        await a.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["processor", "start"])
async def test_optional_knowledge_failure_does_not_undo_preflight(app, monkeypatch, failure_stage):
    import types

    import frameseek.knowledge_jobs
    import frameseek.routes

    pending = []

    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

    def fail(*args, **kwargs):
        raise RuntimeError("optional indexing unavailable")

    monkeypatch.setattr(frameseek.routes, "threading", types.SimpleNamespace(Thread=DeferredThread))
    monkeypatch.setattr(frameseek.routes, "probe", lambda *_: {})
    monkeypatch.setattr(
        frameseek.routes,
        "scene_windows",
        lambda *_: {"duration": 8, "windows": [{"start": 0, "end": 8}]},
    )
    monkeypatch.setattr(frameseek.routes, "frames", lambda *_: [])
    monkeypatch.setattr(frameseek.routes, "local_configured", lambda: True)
    monkeypatch.setattr(
        frameseek.knowledge_jobs.FixtureKnowledgeJobs,
        "processor",
        fail if failure_stage == "processor" else lambda *_: object(),
    )
    if failure_stage == "start":
        monkeypatch.setattr(frameseek.knowledge_jobs.FixtureKnowledgeJobs, "start", fail)
    app.state.knowledge_fixture_refactor = True
    a = await client_for(app)
    try:
        uploaded = await a.post(
            "/api/videos", content=b"deferred", headers={"content-type": "video/mp4"}
        )
        video = uploaded.json()["id"]
        worker = pending.pop()
        worker.target(*worker.args)
        detail = (await a.get(f"/api/videos/{video}")).json()
        assert detail["job"]["state"] == "ready"
        assert detail["job"]["error"] is None and detail["job"]["lease"] is None
        assert detail["metadata"]["duration"] == 8
    finally:
        await a.aclose()


def test_restart_recovers_queued_jobs_without_replaying(tmp_path):
    ws = Workspace(tmp_path)
    with ws.db() as c:
        c.execute(
            "INSERT INTO jobs(id,video,generation,state,lease,updated) "
            "VALUES('j','v',1,'queued',?,?)",
            (time.time() + 3600, time.time()),
        )
    ws = Workspace(tmp_path)
    with ws.db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id='j'").fetchone()
    assert row["state"] == "failed" and row["error"] == "interrupted" and row["lease"] is None


def test_restart_distinguishes_saved_replies_from_unknown_dispatches(tmp_path):
    ws = Workspace(tmp_path)
    key = "a" * 64
    for video in ("before", "saved", "unknown", "missing"):
        (tmp_path / video).mkdir()
    for video in ("saved", "unknown"):
        directory = tmp_path / video / f"knowledge-g1-{key}"
        directory.mkdir()
        (directory / "window-0000.dispatched.json").write_text("{}")
    (tmp_path / "saved" / f"knowledge-g1-{key}" / "window-0000.raw.json").write_text("{}")
    with ws.db() as c:
        for video in ("before", "saved", "unknown", "missing"):
            c.execute(
                "INSERT INTO knowledge_jobs(video,generation,state,updated,cache_key) "
                "VALUES(?,1,'indexing',?,?)",
                (video, time.time(), None if video == "before" else key),
            )
    reopened = Workspace(tmp_path)
    with reopened.db() as c:
        states = {
            r["video"]: (r["state"], r["error"])
            for r in c.execute("SELECT video,state,error FROM knowledge_jobs")
        }
    assert states["before"] == ("failed", "interrupted_resumable")
    assert states["saved"] == ("failed", "interrupted_resumable")
    assert states["unknown"] == ("unknown", "interrupted")
    assert states["missing"] == ("unknown", "interrupted")


@pytest.mark.anyio
async def test_real_upload_probe_private_frames_range_delete_and_cascade(app):
    a, b = await client_for(app), await client_for(app)
    content = Path("src/frameseek/static/demo/parcel.mp4").read_bytes()
    try:
        uploaded = await a.post(
            "/api/videos",
            content=content,
            headers={"content-type": "video/mp4", "x-file-name": "../../test.mp4"},
        )
        assert uploaded.status_code == 201
        video = uploaded.json()["id"]
        for _ in range(100):
            detail = (await a.get("/api/videos/" + video)).json()
            if detail["job"]["state"] not in {"queued", "probing"}:
                break
            await asyncio.sleep(0.05)
        assert detail["name"] == "test.mp4"
        renamed = await a.patch("/api/videos/" + video, json={"name": "仓库片段 A"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "仓库片段 A"
        assert (await b.patch("/api/videos/" + video, json={"name": "侵入"})).status_code == 404
        assert detail["job"]["state"] == "awaiting_configuration", detail
        assert detail["metadata"]["duration"] == 18
        preview = detail["metadata"]["preview"]
        assert len(preview) == 8 and preview[-1]["t"] > 15
        assert (await a.get(preview[0]["uri"])).status_code == 200
        assert (await b.get(preview[0]["uri"])).status_code == 404
        media = "/media/" + video + "/video"
        ranged = await a.get(media, headers={"range": "bytes=0-31"})
        assert ranged.status_code == 206
        assert ranged.headers["content-type"] == "video/mp4"
        assert ranged.content == content[:32]
        assert (await b.get(media)).status_code == 404
        desk = (await a.post("/api/investigations", json={"video_id": video})).json()
        question = {
            "investigation_id": desk["id"],
            "request_id": "blocked_basketball_question",
            "generation": 0,
            "question": "谁投的3分最多？",
        }
        prepared = (await a.post("/api/chat/prepare", json=question)).json()
        assert prepared["kind"] == "runtime_blocked"
        assert "not sent to a model" in prepared["message"]
        assert (await a.post("/api/chat", json=question)).status_code == 409
        with app.state.workspace.db() as c:
            assert c.execute("SELECT count(*) FROM queries").fetchone()[0] == 0
        assert (await a.post(f"/api/videos/{video}/analyze")).status_code == 409
        assert (await a.delete("/api/videos/" + video)).status_code == 204
        assert (await a.get(media)).status_code == 410
        assert (await a.get("/api/investigations/" + desk["id"])).status_code == 410
        assert not (app.state.workspace.root / video).exists()
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.anyio
async def test_malformed_and_oversize_upload_no_model_dispatch(app):
    a = await client_for(app)
    try:
        huge = await a.post(
            "/api/videos",
            content=b"x",
            headers={"content-type": "video/mp4", "content-length": str(600 * 1024**2)},
        )
        assert huge.status_code == 413
        bad = await a.post(
            "/api/videos", content=b"not-a-video", headers={"content-type": "video/mp4"}
        )
        video = bad.json()["id"]
        for _ in range(50):
            result = (await a.get("/api/videos/" + video)).json()
            if result["job"]["state"] == "failed":
                break
            await asyncio.sleep(0.05)
        assert result["job"]["state"] == "failed"
        assert result["job"]["error"] == "invalid_media"
    finally:
        await a.aclose()


def test_expiry_and_generation_cannot_publish_late_manifest(tmp_path):
    ws = Workspace(tmp_path)
    owner = "owner_a"
    with ws.db() as c:
        c.execute(
            "INSERT INTO videos(id,owner,name,bytes,expires) VALUES('v',?,'v',1,?)",
            (owner, time.time() + 60),
        )
        c.execute(
            "INSERT INTO jobs(id,video,generation,state,updated) VALUES('j','v',1,'indexing',?)",
            (time.time(),),
        )
    desk = ws.create(owner, "v")
    ws.delete("videos", "v", owner)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        ws.publish("v", owner, 1, {"segments": [1]})
    assert ws.list("investigations", owner) == []
    with ws.db() as c:
        assert (
            c.execute("SELECT state FROM investigations WHERE id=?", (desk["id"],)).fetchone()[0]
            == "{}"
        )


@pytest.mark.anyio
async def test_cross_video_and_forged_real_evidence_cannot_be_saved(app):
    a = await client_for(app)
    try:
        desk = (await a.post("/api/investigations", json={})).json()
        catalog = (await a.get("/demo/catalog")).json()
        query = catalog["queries"][0]
        altered = json.loads(json.dumps(query))
        event = altered["response"]["events"][0]
        event["video_id"] = "other"
        for frame in event["evidence"]:
            frame["uri"] = frame["uri"].replace("demo_parcel", "other")
            frame["frame_id"] = frame["frame_id"].replace("demo_parcel", "other")
        response = await a.put(
            "/api/investigations/" + desk["id"],
            json={"revision": 0, "state": {"records": [{"id": "task-1", "query": altered}]}},
        )
        assert response.status_code == 422
    finally:
        await a.aclose()


@pytest.mark.anyio
async def test_basketball_demo_is_isolated_and_legacy_remains_restorable(app):
    a, b = await client_for(app), await client_for(app)
    try:
        before = (await a.get("/health")).json()["model_requests_sent"]
        basketball = (await a.get("/demo/catalog?video_id=demo_basketball")).json()
        legacy = (await a.get("/demo/catalog")).json()
        assert legacy["video"]["id"] == "demo_parcel"
        assert (await a.get("/demo/catalog?video_id=unknown")).status_code == 404
        assert (await a.get("/demo/video?video_id=unknown")).status_code == 404
        assert (
            await a.get("/demo/video?video_id=demo_basketball", headers={"Range": "bytes=0-31"})
        ).status_code == 206
        desks = {}
        for video, query in (
            ("demo_basketball", basketball["queries"][0]),
            ("demo_parcel", legacy["queries"][0]),
        ):
            desk = (await a.post("/api/investigations", json={"video_id": video})).json()
            path = "/api/investigations/" + desk["id"]
            state = {"records": [{"id": "task-1", "query": query}]}
            assert (await a.put(path, json={"revision": 0, "state": state})).status_code == 200
            assert (await a.get(path)).json()["state"]["records"][0]["query"] == query
            assert (await b.get(path)).status_code == 404
            desks[video] = (path, state)
        foreign = {"records": [{"id": "task-1", "query": basketball["queries"][0]}]}
        assert (
            await a.put(desks["demo_parcel"][0], json={"revision": 1, "state": foreign})
        ).status_code == 422
        assert (await a.get("/health")).json()["model_requests_sent"] == before
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.anyio
async def test_local_session_reissue_preserves_owner_csrf_retention_and_private_state(app):
    a = await client_for(app)
    try:
        token = a.cookies.get("fs_session")
        original = app.state.workspace.authenticate(token)
        desk = (await a.post("/api/investigations", json={})).json()
        state = {"title": "保留原调查", "records": [], "ui": {"draft": "未发送的草稿"}}
        path = "/api/investigations/" + desk["id"]
        assert (await a.put(path, json={"revision": 0, "state": state})).status_code == 200
        replacement = app.state.workspace.reissue_local_session(original["id"])
        assert replacement != token
        assert app.state.workspace.authenticate(replacement) == original
        assert app.state.workspace.session(replacement) == (original, None)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as recovered:
            recovered.cookies.set("fs_session", replacement)
            session = (await recovered.get("/api/session")).json()
            assert session["owner_id"] == original["id"] and session["csrf"] == original["csrf"]
            assert (await recovered.get(path)).json()["state"] == state
            assert (
                await recovered.put(path, json={"revision": 1, "state": state})
            ).status_code == 403
        newer = app.state.workspace.reissue_local_session(original["id"])
        assert app.state.workspace.authenticate(newer) == original
        with pytest.raises(HTTPException) as invalid:
            app.state.workspace.authenticate(replacement)
        assert invalid.value.status_code == 401
        with app.state.workspace.db() as c:
            c.execute("UPDATE sessions SET expires=0 WHERE id=?", (original["id"],))
        with pytest.raises(HTTPException) as expired:
            app.state.workspace.reissue_local_session(original["id"])
        assert expired.value.status_code == 401
        with pytest.raises(HTTPException):
            app.state.workspace.authenticate(newer)
    finally:
        await a.aclose()
