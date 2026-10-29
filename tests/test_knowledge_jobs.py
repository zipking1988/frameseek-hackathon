"""Fixture-only upload -> checkpoint -> chat integration; never a live model."""

import asyncio
import json
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from test_knowledge_compiler import infer
from test_knowledge_supplement import FixtureSupplement

from frameseek.api import create_app
from frameseek.config import Settings
from frameseek.knowledge_jobs import failure_state


def test_stale_workers_leave_terminal_job_states_without_dispatch(tmp_path):
    import sqlite3
    from types import SimpleNamespace

    from fastapi import HTTPException

    from frameseek.knowledge_jobs import FixtureKnowledgeJobs

    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as c:
        c.executescript(
            "CREATE TABLE knowledge_supplement_jobs "
            "(owner TEXT, request_id TEXT, state TEXT, error TEXT, updated REAL);"
            "CREATE TABLE knowledge_jobs "
            "(video TEXT, generation INTEGER, run_id TEXT, state TEXT, error TEXT, updated REAL);"
            "INSERT INTO knowledge_supplement_jobs VALUES ('owner','request','running',NULL,0);"
            "INSERT INTO knowledge_jobs VALUES ('video',1,'run','queued',NULL,0);"
        )

    class StaleWorkspace:
        def db(self):
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            return connection

        def owned(self, *_args):
            raise HTTPException(404, "expired or removed")

    jobs = FixtureKnowledgeJobs(StaleWorkspace(), None, None)
    source = {"id": "video", "generation": 1}
    jobs._supplement_work(
        source,
        {"id": "desk"},
        "owner",
        SimpleNamespace(request_id="request", generation=0),
        "question",
        {},
        object(),
    )
    jobs._work(source, "owner", {}, object(), "run")
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT state FROM knowledge_supplement_jobs").fetchone()[0] == "cancelled"
        assert c.execute("SELECT state FROM knowledge_jobs").fetchone()[0] == "cancelled"


def test_unknown_dispatch_stays_unknown_if_context_changes_during_failure(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from fastapi import HTTPException

    import frameseek.knowledge_jobs as job_module
    import frameseek.knowledge_supplement as supplement_module
    import frameseek.video_knowledge as knowledge_module

    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as c:
        c.executescript(
            "CREATE TABLE knowledge_supplement_jobs "
            "(owner TEXT, request_id TEXT, state TEXT, error TEXT, updated REAL, "
            "investigation TEXT);"
            "CREATE TABLE knowledge_jobs "
            "(video TEXT, generation INTEGER, state TEXT, cache_key TEXT, version TEXT);"
            "INSERT INTO knowledge_supplement_jobs "
            "VALUES ('owner','request','running',NULL,0,'desk');"
            "INSERT INTO knowledge_jobs VALUES ('video',1,'ready','key','version');"
        )

    class Workspace:
        root = tmp_path
        stale = False

        def db(self):
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            return connection

        def owned(self, table, *_args):
            if self.stale:
                raise HTTPException(404, "changed")
            return (
                {"generation": 1}
                if table == "videos"
                else {"video": "video", "state": '{"epoch":0}'}
            )

        def owner_active(self, _owner):
            return True

    ws = Workspace()
    monkeypatch.setattr(job_module, "source_digest", lambda *_: "digest")
    monkeypatch.setattr(knowledge_module, "load_published_state", lambda *_: object())
    monkeypatch.setattr(job_module, "KnowledgeStore", lambda *_: object())

    def uncertain_dispatch(*_args, **_kwargs):
        ws.stale = True
        raise ValueError("knowledge_dispatch_unknown")

    monkeypatch.setattr(supplement_module, "compile_local_supplement", uncertain_dispatch)
    jobs = job_module.FixtureKnowledgeJobs(ws, None, None)
    jobs._supplement_work(
        {"id": "video", "generation": 1},
        {"id": "desk"},
        "owner",
        SimpleNamespace(request_id="request", generation=0),
        "question",
        {"knowledge_version": "version"},
        object(),
    )
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT state,error FROM knowledge_supplement_jobs").fetchone() == (
            "unknown",
            "knowledge_dispatch_unknown",
        )
    ws.stale = False
    assert jobs.cancel_supplement("desk", "owner", "request") == "unknown"
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT state,error FROM knowledge_supplement_jobs").fetchone() == (
            "unknown",
            "knowledge_dispatch_unknown",
        )


def test_base_publication_rejected_by_expiry_does_not_stay_indexing(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    import frameseek.knowledge_jobs as job_module

    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as c:
        c.executescript(
            "CREATE TABLE knowledge_jobs "
            "(video TEXT, generation INTEGER, run_id TEXT, state TEXT, error TEXT, updated REAL, "
            "cache_key TEXT, version TEXT, total INTEGER, completed INTEGER);"
            "CREATE TABLE videos (id TEXT, generation INTEGER, deleted INTEGER, expires REAL);"
            "CREATE TABLE sessions (id TEXT, expires REAL);"
            "INSERT INTO knowledge_jobs VALUES ('video',1,'run','queued',NULL,0,NULL,NULL,1,0);"
            "INSERT INTO videos VALUES ('video',1,0,0);"
            "INSERT INTO sessions VALUES ('owner',9999999999);"
        )

    class Workspace:
        root = tmp_path

        def db(self):
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            return connection

        def owned(self, *_args):
            # Simulate expiry after the worker's active check but before the
            # guarded publication UPDATE, which checks the actual database.
            return {"generation": 1}

        def owner_active(self, _owner):
            return True

    monkeypatch.setattr(job_module, "source_digest", lambda *_: "digest")
    monkeypatch.setattr(job_module, "VideoKnowledge", lambda **_: object())
    monkeypatch.setattr(job_module, "KnowledgeStore", lambda *_: SimpleNamespace(key="key"))
    monkeypatch.setattr(job_module, "compile_pending", lambda *_args, **_kwargs: "version")
    jobs = job_module.FixtureKnowledgeJobs(Workspace(), None, None)
    jobs._work(
        {"id": "video", "generation": 1},
        "owner",
        {"duration": 1},
        SimpleNamespace(revision="fixture", extraction_config="fixture", infer=lambda *_: None),
        "run",
    )
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT state,version FROM knowledge_jobs").fetchone() == (
            "cancelled",
            None,
        )


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FixtureProcessor:
    fixture_only = True
    revision = "synthetic-fixture-v1"
    extraction_config = "8-second-sampling"

    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def infer(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("fixture_timeout")
        if args[0].startswith(("Review each numbered image", "Inspect only this image")):
            return FixtureSupplement().infer(*args, **kwargs)
        result = infer(*args, **kwargs)
        data = json.loads(result["text"])
        data["frames"][0]["subjects"].append(
            {
                "id": "visible-text",
                "facts": [{"field": "text", "value": "SYNTHETIC LABEL WHEN 01:00-02:00"}],
            }
        )
        return {"text": json.dumps(data)}


@pytest.mark.anyio
async def test_auto_chat_plans_bounded_review_for_unidentified_subject_alongside_hit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "workspace"))
    for key in ("FS_RUNTIME_FILE", "FS_PUBLIC_ORIGIN", "FS_CLOUD_VISION", "FS_LOCAL_VISION"):
        monkeypatch.delenv(key, raising=False)

    class MixedSubjects(FixtureProcessor):
        def infer(self, *args, **kwargs):
            result = super().infer(*args, **kwargs)
            data = json.loads(result["text"])
            data["frames"][0]["subjects"][0]["facts"].append(
                {"field": "garment", "value": "blue jersey"}
            )
            data["frames"][0]["subjects"].append(
                {"id": "unidentified", "facts": [{"field": "pose", "value": "standing"}]}
            )
            return {"text": json.dumps(data)}

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    clip = tmp_path / "mixed-subjects.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x72:rate=2",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
        timeout=20,
    )
    app = create_app(Settings(mode="fixture"))
    processor = MixedSubjects()
    processor.release.set()
    app.state.knowledge_fixture_refactor = True
    app.state.knowledge_fixture_processor = processor
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = (await client.get("/api/session")).json()
        client.headers["x-fs-csrf"] = session["csrf"]
        response = await client.post(
            "/api/videos", content=clip.read_bytes(), headers={"content-type": "video/mp4"}
        )
        assert response.status_code == 201
        video = response.json()["id"]
        for _ in range(500):
            status = (await client.get(f"/api/videos/{video}/knowledge")).json()
            if status["state"] in {"ready", "failed", "unknown"}:
                break
            await asyncio.sleep(0.02)
        assert status["state"] == "ready", status
        desk = (await client.post("/api/investigations", json={"video_id": video})).json()
        body = {
            "investigation_id": desk["id"],
            "request_id": "mixed-subjects",
            "generation": 0,
            "question": "请在00:00至00:01找出穿蓝色球衣的人，列出画面。",
        }
        count = processor.calls
        prepared = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert prepared["kind"] == "supplement_required", prepared
        assert prepared["has_index_matches"] is True
        assert (
            sum(len(window["frame_ids"]) for window in prepared["supplement_plan"]["windows"]) == 1
        )
        assert prepared["model_requests_sent"] == 0
        assert processor.calls == count

        # A single-condition time query reuses its existing frame match.
        for request_id, question in (("single-scoped", "请在00:00至00:01找出人"),):
            prepared = (
                await client.post(
                    "/api/auto-chat/prepare",
                    json=body | {"request_id": request_id, "question": question},
                )
            ).json()
            assert prepared["kind"] == "ready", prepared
            assert prepared["engine"] == "index"
            assert prepared["model_requests_sent"] == 0
            assert processor.calls == count


@pytest.mark.anyio
async def test_repeated_question_reuses_completed_supplement_with_remaining_candidates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "workspace"))
    for key in ("FS_RUNTIME_FILE", "FS_PUBLIC_ORIGIN", "FS_CLOUD_VISION", "FS_LOCAL_VISION"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Settings(mode="fixture"))
    processor = FixtureProcessor()
    processor.release.set()
    app.state.knowledge_fixture_refactor = True
    app.state.knowledge_fixture_processor = processor
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    clip = tmp_path / "three-windows.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x72:rate=2",
            "-t",
            "24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
        timeout=20,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = (await client.get("/api/session")).json()
        client.headers["x-fs-csrf"] = session["csrf"]
        video = (
            await client.post(
                "/api/videos", content=clip.read_bytes(), headers={"content-type": "video/mp4"}
            )
        ).json()["id"]
        for _ in range(500):
            status = (await client.get(f"/api/videos/{video}/knowledge")).json()
            if status["state"] in {"ready", "failed", "unknown"}:
                break
            await asyncio.sleep(0.02)
        assert status["state"] == "ready", status
        desk = (await client.post("/api/investigations", json={"video_id": video})).json()
        body = {
            "investigation_id": desk["id"],
            "request_id": "first-detail",
            "generation": 0,
            "question": "find people wearing red hat",
        }
        plan = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert plan["kind"] == "supplement_required"
        assert len(plan["supplement_plan"]["windows"]) == 2
        processor.release.clear()
        processor.entered.clear()
        assert (await client.post("/api/knowledge-chat/supplement", json=body)).status_code == 202
        for _ in range(100):
            if processor.entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert processor.entered.is_set()
        conflicting = body | {"engine": "index", "question": "find people"}
        rejected = await client.post("/api/index-chat", json=conflicting)
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "request_payload_conflict"
        pending = await client.post("/api/index-chat", json=body | {"engine": "index"})
        assert pending.status_code == 409
        with app.state.workspace.db() as connection:
            assert not connection.execute(
                "SELECT 1 FROM queries WHERE owner=? AND id=?",
                (session["owner_id"], body["request_id"]),
            ).fetchone()
        processor.release.set()
        for _ in range(500):
            done = (
                await client.get(f"/api/investigations/{desk['id']}/supplements/first-detail")
            ).json()
            if done["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert done["state"] == "done", done
        rejected = await client.post("/api/index-chat", json=conflicting)
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "request_payload_conflict"
        calls = processor.calls
        repeated = body | {"request_id": "repeat-detail"}
        response = (await client.post("/api/auto-chat/prepare", json=repeated)).json()
        assert response["kind"] == "ready" and response["plan_kind"] == "direct_retrieval", response
        answer = await client.post("/api/index-chat", json=repeated | {"engine": "index"})
        assert answer.status_code == 200 and answer.json()["response"]["events"]
        assert processor.calls == calls
        other_context = repeated | {
            "request_id": "different-context",
            "parent_request_id": "repeat-detail",
            "question": "find all people wearing red hat",
        }
        prepared = (await client.post("/api/auto-chat/prepare", json=other_context)).json()
        assert prepared["kind"] == "supplement_required", prepared
        assert prepared["has_index_matches"] is True
        assert processor.calls == calls

        def empty_supplement(prompt, paths, schema=None):
            processor.calls += 1
            return {
                "text": json.dumps(
                    {"frames": [{"index": "0", "description": "no match", "subjects": []}]}
                )
            }

        processor.infer = empty_supplement
        negative = body | {
            "request_id": "no-green-hat",
            "question": "find people wearing green hat from 00:00 to 00:01",
        }
        assert (await client.post("/api/auto-chat/prepare", json=negative)).json()["kind"] == (
            "supplement_required"
        )
        assert (
            await client.post("/api/knowledge-chat/supplement", json=negative)
        ).status_code == 202
        for _ in range(500):
            negative_job = (
                await client.get(f"/api/investigations/{desk['id']}/supplements/no-green-hat")
            ).json()
            if negative_job["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert negative_job["state"] == "done", negative_job
        after_negative = processor.calls
        no_match = (await client.post("/api/auto-chat/prepare", json=negative)).json()
        assert no_match["kind"] == "ready" and no_match["plan_kind"] == "supplement_unconfirmed"
        assert (
            await client.post("/api/index-chat/prepare", json=negative | {"engine": "index"})
        ).json()["kind"] == "ready"
        terminal = await client.post("/api/index-chat", json=negative | {"engine": "index"})
        assert terminal.status_code == 200, terminal.text
        answer = terminal.json()
        assert answer["response"]["status"] == "no_result"
        assert not answer["response"]["events"]
        assert "相关画面已补查" in answer["response"]["answer"]
        assert "不能断定视频中不存在" in answer["response"]["answer"]
        assert answer["index_search"]["supplemented"] is True
        assert answer["response"]["trace"]["visible"]["segments_reviewed"] == 1
        checked = answer["index_search"]["checked_frames"]
        assert len(checked) == 1 and checked[0]["t"] == 0
        assert checked[0]["uri"].startswith(f"/media/{video}/frames/")
        other_question = negative | {
            "request_id": "different-negative",
            "question": "find people wearing purple hat from 00:00 to 00:01",
        }
        assert (await client.post("/api/auto-chat/prepare", json=other_question)).json()[
            "kind"
        ] == "supplement_required"
        assert (
            await client.post("/api/knowledge-chat/supplement", json=other_question)
        ).status_code == 202
        for _ in range(500):
            newer = (
                await client.get(f"/api/investigations/{desk['id']}/supplements/different-negative")
            ).json()
            if newer["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert newer["state"] == "done", newer
        assert newer["version"] != negative_job["version"]
        after_negative = processor.calls
        with app.state.workspace.db() as connection:
            cache_key = connection.execute(
                "SELECT cache_key FROM knowledge_jobs WHERE video=? AND generation=1", (video,)
            ).fetchone()["cache_key"]
        prior_version = (
            app.state.workspace.root
            / video
            / f"knowledge-g1-{cache_key}"
            / f"version-{negative_job['version']}.json"
        )
        unavailable = prior_version.with_suffix(".temporarily-absent")
        prior_version.rename(unavailable)
        try:
            rejected = await client.post(
                "/api/auto-chat/prepare", json=negative | {"request_id": "unverifiable-prior"}
            )
            assert rejected.status_code == 409
            assert rejected.json()["detail"] == "observation_index_unavailable_or_stale"
            assert processor.calls == after_negative
        finally:
            unavailable.rename(prior_version)
        repeated_negative = negative | {"request_id": "no-green-hat-again"}
        assert (await client.post("/api/auto-chat/prepare", json=repeated_negative)).json()[
            "kind"
        ] == "ready"
        cached = await client.post("/api/index-chat", json=repeated_negative | {"engine": "index"})
        assert cached.status_code == 200
        assert cached.json()["index_search"]["supplement_reused"] is True
        assert cached.json()["index_search"]["checked_frames"] == checked
        assert "本次未调用模型" in cached.json()["response"]["answer"]
        assert processor.calls == after_negative
        original_again = body | {"request_id": "original-after-new-version"}
        previous = (await client.post("/api/auto-chat/prepare", json=original_again)).json()
        assert previous["kind"] == "ready" and previous["plan_kind"] == "direct_retrieval"
        assert processor.calls == after_negative
        frame_file = (
            app.state.workspace.root / video / "frames" / checked[0]["uri"].rsplit("/", 1)[-1]
        )
        saved_frame = frame_file.with_suffix(".temporarily-absent")
        frame_file.rename(saved_frame)
        try:
            missing_frame = await client.post(
                "/api/index-chat",
                json=negative | {"request_id": "missing-frame", "engine": "index"},
            )
            assert missing_frame.status_code == 409
            assert missing_frame.json()["detail"] == "observation_index_unavailable_or_stale"
        finally:
            saved_frame.rename(frame_file)

        entered, release_unknown = threading.Event(), threading.Event()

        def unknown_response(*args, **kwargs):
            entered.set()
            assert release_unknown.wait(10)
            raise RuntimeError("unknown response")

        processor.infer = unknown_response
        assert (
            await client.post("/api/knowledge-chat/supplement", json=other_context)
        ).status_code == 202
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        still_running = await client.post(
            "/api/index-chat", json=other_context | {"engine": "index"}
        )
        assert still_running.status_code == 409
        assert still_running.json()["detail"] == "supplement_still_running"
        release_unknown.set()
        for _ in range(500):
            failed = (
                await client.get(f"/api/investigations/{desk['id']}/supplements/different-context")
            ).json()
            if failed["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert failed["state"] == "unknown", failed
        partial = await client.post("/api/index-chat", json=other_context | {"engine": "index"})
        assert partial.status_code == 200 and partial.json()["response"]["events"]
        assert partial.json()["index_search"]["supplement_incomplete"] is True
        assert "补查未完成或结果不明" in partial.json()["response"]["answer"]


def test_interrupted_dispatch_is_not_reported_as_retryable_failure():
    assert failure_state("local_model_worker_stopped") == "unknown"
    assert failure_state("local_model_timeout_unknown") == "unknown"
    assert failure_state("knowledge_dispatch_unknown") == "unknown"
    assert failure_state("local_observation_invalid") == "failed"


def test_local_resume_selects_old_prompt_only_for_matching_identity():
    from types import SimpleNamespace

    from frameseek.knowledge_jobs import select_local_resume_processor
    from frameseek.video_knowledge import VideoKnowledge

    processor = SimpleNamespace(
        local_only=True,
        revision="local-a",
        extraction_config="knowledge-v5-objects-8sec-8frames-4096tokens",
    )
    metadata = {"duration": 16, "scene_boundaries": [4]}
    digest = "a" * 64
    old = VideoKnowledge(
        video="vid_fixture",
        generation=1,
        duration=16,
        source_fingerprint=digest,
        processor_revision="local-a",
        extraction_config="knowledge-v3-8sec-8frames-4096tokens",
        scene_boundaries=[4],
    )
    resumed = select_local_resume_processor(
        processor, "vid_fixture", 1, metadata, digest, old.cache_key()
    )
    assert resumed is not processor
    assert resumed.extraction_config == old.extraction_config
    assert processor.extraction_config.startswith("knowledge-v5-")
    v4 = old.model_copy(update={"extraction_config": "knowledge-v4-bound-8sec-8frames-4096tokens"})
    assert (
        select_local_resume_processor(
            processor, "vid_fixture", 1, metadata, digest, v4.cache_key()
        ).extraction_config
        == v4.extraction_config
    )
    assert (
        select_local_resume_processor(
            processor, "vid_fixture", 2, metadata, digest, old.cache_key()
        )
        is processor
    )
    assert (
        select_local_resume_processor(
            processor, "vid_fixture", 1, metadata, "b" * 64, old.cache_key()
        )
        is processor
    )
    assert (
        select_local_resume_processor(processor, "vid_fixture", 1, metadata, digest, None)
        is processor
    )


@pytest.mark.anyio
async def test_cloud_opt_in_does_not_bypass_knowledge_when_provider_mismatches(
    tmp_path, monkeypatch
):
    from test_gemini import configuration

    import frameseek.api

    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("FS_KNOWLEDGE_TOKENFACTORY", "1")
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic-only")
    monkeypatch.delenv("FS_PUBLIC_ORIGIN", raising=False)
    monkeypatch.setattr(frameseek.api, "load_runtime", configuration)
    app = create_app(Settings(mode="real"))
    calls = []
    app.state.agent.transport = httpx.MockTransport(lambda request: calls.append(request))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = (await client.get("/api/session")).json()
        client.headers["x-fs-csrf"] = session["csrf"]
        video = (
            await client.post(
                "/api/videos",
                content=Path("src/frameseek/static/demo/parcel.mp4").read_bytes(),
                headers={"content-type": "video/mp4"},
            )
        ).json()["id"]
        desk = (await client.post("/api/investigations", json={"video_id": video})).json()
        body = {
            "investigation_id": desk["id"],
            "request_id": "q1",
            "generation": 0,
            "question": "find a box",
        }
        status = (await client.get(f"/api/videos/{video}/knowledge")).json()
        assert status["required_for_chat"]
        assert (await client.post("/api/chat", json=body)).status_code == 409
        prepared = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert prepared["kind"] == "knowledge_not_ready"
        assert prepared["model_requests_sent"] == 0 and not calls


@pytest.mark.anyio
@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize(
    "contract",
    [
        "video_knowledge_extraction_v3",
        "video_knowledge_extraction_v4",
        "video_knowledge_extraction_v5",
    ],
)
async def test_cloud_upload_index_and_chat_with_mock_transport(
    tmp_path, monkeypatch, approved, contract
):
    from test_agent import approve
    from test_tokenfactory import configuration

    import frameseek.api
    from frameseek.knowledge_budget import ExtractionCalibration

    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("FS_KNOWLEDGE_TOKENFACTORY", "1")
    monkeypatch.setenv("TEST_TF_KEY", "synthetic-only")
    monkeypatch.delenv("FS_PUBLIC_ORIGIN", raising=False)
    runtime = configuration()
    monkeypatch.setattr(frameseek.api, "load_runtime", lambda: runtime)
    calibration = ExtractionCalibration(
        reference="synthetic test only",
        processor_revision=runtime.vision.revision,
        contract=contract,
        max_frames=8,
        max_output_tokens=4096,
        max_cost_units=1,
        currency_unit="USD_MICRO",
        valid_until=time.time() + 120,
    )
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(asdict(calibration)))
    monkeypatch.setenv("FS_KNOWLEDGE_CALIBRATION_FILE", str(calibration_path))
    supplement_calibration_path = tmp_path / "supplement-calibration.json"
    supplement_calibration_path.write_text(
        json.dumps(asdict(calibration) | {"contract": "video_knowledge_supplement_v6"})
    )
    monkeypatch.setenv("FS_KNOWLEDGE_SUPPLEMENT_CALIBRATION_FILE", str(supplement_calibration_path))
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        count = sum(x.get("type") == "image_url" for x in payload["messages"][1]["content"])
        prompt = payload["messages"][1]["content"][0]["text"]
        if prompt.startswith("Review each numbered image"):
            result = FixtureSupplement().infer("fixture", [None], {})
        elif prompt.startswith("Inspect only this image"):
            result = FixtureSupplement().infer("fixture", [None], {"properties": {"visible": {}}})
        else:
            result = infer("fixture", [None] * count, None)
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": result["text"]}, "finish_reason": "stop"}]},
        )

    app = create_app(Settings(mode="real"))
    app.state.agent.transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = (await client.get("/api/session")).json()
        client.headers["x-fs-csrf"] = session["csrf"]
        if approved:
            approve(app.state.agent.ledger, session["owner_id"], calls=20, money=20)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("FFmpeg required")
        clip = tmp_path / "synthetic-cloud.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=128x72:rate=2",
                "-t",
                "2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
            timeout=20,
        )
        upload = await client.post(
            "/api/videos", content=clip.read_bytes(), headers={"content-type": "video/mp4"}
        )
        assert upload.status_code == 201
        video = upload.json()["id"]
        for _ in range(500):
            status = (await client.get(f"/api/videos/{video}/knowledge")).json()
            if status["state"] in {"ready", "failed", "unknown", "waiting_budget"}:
                break
            await asyncio.sleep(0.02)
        if not approved:
            assert status["state"] == "waiting_budget", status
            assert not calls
            return
        assert status["state"] == "ready", status
        assert status["processor"] == "tokenfactory" and len(calls) == 1
        prompt = json.loads(calls[0].content)["messages"][1]["content"][0]["text"]
        assert ("garment=blue jersey" in prompt) == (not contract.endswith("v3"))
        assert ("include its color" in prompt) == contract.endswith("v5")
        desk = (
            await client.post("/api/investigations", json={"video_id": video, "title": "fixture"})
        ).json()
        for request_id in ("cloud-1", "cloud-2"):
            body = {
                "investigation_id": desk["id"],
                "request_id": request_id,
                "generation": 0,
                "question": "有没有人物？",
            }
            assert (await client.post("/api/auto-chat/prepare", json=body)).json()[
                "kind"
            ] == "ready"
            answer = await client.post("/api/index-chat", json=body | {"engine": "index"})
            assert answer.status_code == 200 and answer.json()["response"]["events"]
        assert len(calls) == 1
        body = {
            "investigation_id": desk["id"],
            "request_id": "cloud-detail",
            "generation": 0,
            "question": "find people wearing red hat",
        }
        monkeypatch.delenv("FS_KNOWLEDGE_SUPPLEMENT_CALIBRATION_FILE")
        blocked = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert blocked["kind"] == "budget_blocked" and blocked["model_requests_sent"] == 0
        assert len(calls) == 1
        monkeypatch.setenv(
            "FS_KNOWLEDGE_SUPPLEMENT_CALIBRATION_FILE", str(supplement_calibration_path)
        )
        prepared = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert prepared["kind"] == "supplement_required", prepared
        assert prepared["estimated_new_calls"] > 0
        assert prepared["estimated_max_cost_units"] == prepared["estimated_new_calls"]
        assert prepared["model_requests_sent"] == 0 and len(calls) == 1
        started = await client.post("/api/knowledge-chat/supplement", json=body)
        assert started.status_code == 202, started.text
        for _ in range(300):
            supplement = (
                await client.get(f"/api/investigations/{desk['id']}/supplements/cloud-detail")
            ).json()
            if supplement["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert supplement["state"] == "done", supplement
        recovered = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()
        saved_supplement = next(
            item for item in recovered["supplements"] if item["id"] == "cloud-detail"
        )
        assert saved_supplement["state"] == "done"
        assert saved_supplement["input"]["question"] == body["question"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as stranger:
            await stranger.get("/api/session")
            assert (
                await stranger.get(f"/api/investigations/{desk['id']}/requests")
            ).status_code == 404
        answer = await client.post("/api/index-chat", json=body | {"engine": "index"})
        assert answer.status_code == 200 and "云端视觉" in answer.json()["response"]["answer"]
        assert answer.json()["index_search"]["cloud_requests_sent"] is None
        recovered = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()
        assert not any(item["id"] == "cloud-detail" for item in recovered["supplements"])
        count = len(calls)
        app.state.agent.ledger.restrict("*", runtime.budget_version)
        cached = (
            await client.post(
                "/api/auto-chat/prepare", json=body | {"request_id": "restricted-repeat"}
            )
        ).json()
        assert cached["kind"] == "ready" and cached["plan_kind"] == "direct_retrieval"
        assert cached["model_requests_sent"] == 0 and len(calls) == count
        blocked = (
            await client.post(
                "/api/auto-chat/prepare",
                json=body
                | {"request_id": "restricted-detail", "question": "find people wearing yellow hat"},
            )
        ).json()
        assert blocked["kind"] == "budget_blocked" and blocked["model_requests_sent"] == 0
        assert len(calls) == count


@pytest.mark.anyio
async def test_upload_cancel_resume_and_index_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "workspace"))
    for key in ("FS_RUNTIME_FILE", "FS_PUBLIC_ORIGIN", "FS_CLOUD_VISION", "FS_LOCAL_VISION"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Settings(mode="fixture"))
    processor = FixtureProcessor()
    app.state.knowledge_fixture_refactor = True
    app.state.knowledge_fixture_processor = processor
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        session = (await client.get("/api/session")).json()
        client.headers["x-fs-csrf"] = session["csrf"]
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("FFmpeg required")
        clip = tmp_path / "synthetic.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=128x72:rate=2",
                "-t",
                "12",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
            timeout=20,
        )
        uploaded = await client.post(
            "/api/videos", content=clip.read_bytes(), headers={"content-type": "video/mp4"}
        )
        assert uploaded.status_code == 201
        video = uploaded.json()["id"]
        for _ in range(300):
            if processor.entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert processor.entered.is_set()
        desk_response = await client.post(
            "/api/investigations", json={"video_id": video, "title": "Synthetic test"}
        )
        desk = desk_response.json()
        body = {
            "investigation_id": desk["id"],
            "request_id": "first",
            "generation": 0,
            "question": "find people",
        }
        pending = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert pending["kind"] == "knowledge_not_ready"
        assert (await client.get(f"/media/{video}/video")).status_code == 200
        assert (await client.post("/api/videos/" + video + "/knowledge/cancel")).status_code == 202
        processor.release.set()
        for _ in range(300):
            if list((app.state.workspace.root / video).glob("knowledge-*/window-0000.raw.json")):
                break
            await asyncio.sleep(0.02)
        assert (await client.get(f"/api/videos/{video}")).json()["generation"] == 1
        processor.revision = "different-synthetic-processor"
        assert (await client.post(f"/api/videos/{video}/knowledge/build")).status_code == 202
        for _ in range(300):
            changed = (await client.get(f"/api/videos/{video}/knowledge")).json()
            if changed["state"] in ("failed", "unknown", "ready"):
                break
            await asyncio.sleep(0.02)
        assert changed["state"] == "failed"
        assert changed["error"] == "knowledge_resume_processor_changed"
        assert processor.calls == 1
        processor.revision = "synthetic-fixture-v1"
        previous_config = processor.extraction_config
        processor.extraction_config = "synthetic-different-prompt"
        assert (await client.post(f"/api/videos/{video}/knowledge/build")).status_code == 202
        for _ in range(300):
            changed = (await client.get(f"/api/videos/{video}/knowledge")).json()
            if changed["state"] in ("failed", "unknown", "ready"):
                break
            await asyncio.sleep(0.02)
        assert changed["state"] == "failed"
        assert changed["error"] == "knowledge_resume_processor_changed"
        assert processor.calls == 1
        processor.extraction_config = previous_config
        resumed = await client.post(f"/api/videos/{video}/knowledge/build")
        assert resumed.status_code == 202, resumed.text
        for _ in range(500):
            status = (await client.get(f"/api/videos/{video}/knowledge")).json()
            if status["state"] in ("ready", "failed", "unknown"):
                break
            await asyncio.sleep(0.02)
        assert status["state"] == "ready", status
        assert status["completed_segments"] == status["total_segments"] == 2
        assert processor.calls == 2
        text_point = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={"question": 'Find text "SYNTHETIC" at 00:00', "generation": 1},
        )
        assert text_point.status_code == 200, text_point.text
        assert text_point.json()["ocr_eligible"] is True
        for request in ("first", "second"):
            question = body | {"request_id": request}
            prepared = (await client.post("/api/auto-chat/prepare", json=question)).json()
            assert prepared["kind"] == "ready" and prepared["engine"] == "index", prepared
            result = await client.post("/api/index-chat", json=question | {"engine": "index"})
            assert result.status_code == 200, result.text
            assert result.json()["index_search"]["knowledge_version"] == status["version"]
            assert result.json()["response"]["events"]
            evidence = result.json()["response"]["events"][0]["evidence"][0]
            assert (await client.get(evidence["uri"])).status_code == 200
        natural = body | {
            "request_id": "natural-timestamps",
            "question": "视频里人物出现在哪些时间？请列出画面证据。",
        }
        prepared = (await client.post("/api/auto-chat/prepare", json=natural)).json()
        assert prepared["kind"] == "ready" and prepared["plan_kind"] == "direct_retrieval", prepared
        answer = await client.post("/api/index-chat", json=natural | {"engine": "index"})
        assert answer.status_code == 200 and answer.json()["response"]["events"]
        existence = body | {"request_id": "existence", "question": "视频里有没有人物？"}
        assert (await client.post("/api/auto-chat/prepare", json=existence)).json()[
            "kind"
        ] == "ready"
        answer = await client.post("/api/index-chat", json=existence | {"engine": "index"})
        assert answer.status_code == 200 and answer.json()["response"]["answer"].startswith(
            "索引中观察到有匹配。"
        )
        followup = body | {
            "request_id": "pronoun-followup",
            "question": "它出现在哪些时间？",
            "parent_request_id": "existence",
        }
        assert (await client.post("/api/auto-chat/prepare", json=followup)).json()[
            "kind"
        ] == "ready"
        answer = await client.post("/api/index-chat", json=followup | {"engine": "index"})
        assert answer.status_code == 200 and answer.json()["response"]["events"]
        assert "未确认跨镜头为同一对象" in answer.json()["response"]["answer"]
        assert answer.json()["index_search"]["knowledge_version"] == status["version"]
        retry = body | {
            "request_id": "explicit-followup",
            "question": "继续这个问题",
            "parent_request_id": "existence",
        }
        assert (await client.post("/api/auto-chat/prepare", json=retry)).json()["kind"] == "ready"
        answer = await client.post("/api/index-chat", json=retry | {"engine": "index"})
        assert answer.status_code == 200 and answer.json()["response"]["events"]
        text_query = body | {
            "request_id": "visible-text",
            "question": "找出文字“SYNTHETIC”出现时间",
        }
        prepared = (await client.post("/api/auto-chat/prepare", json=text_query)).json()
        assert prepared["kind"] == "ready" and prepared["plan_kind"] == "direct_retrieval"
        scoped_text = text_query | {
            "request_id": "scoped-visible-text",
            "question": "请在00:00至00:02找出文字“SYNTHETIC”出现时间",
        }
        scoped_plan = (await client.post("/api/auto-chat/prepare", json=scoped_text)).json()
        assert scoped_plan["kind"] == "supplement_required", scoped_plan
        assert scoped_plan["has_index_matches"]
        assert sum(len(w["frame_ids"]) for w in scoped_plan["supplement_plan"]["windows"]) == 1
        natural_text = scoped_text | {
            "request_id": "scoped-visible-text-natural",
            "question": "请在00:00至00:02文字“SYNTHETIC”什么时候出现？",
        }
        natural_plan = (await client.post("/api/auto-chat/prepare", json=natural_text)).json()
        assert natural_plan["kind"] == "supplement_required", natural_plan
        assert natural_plan["has_index_matches"]
        english_text = scoped_text | {
            "request_id": "scoped-visible-text-english",
            "question": 'from 00:00 to 00:02 when does text "SYNTHETIC" appear?',
        }
        english_plan = (await client.post("/api/auto-chat/prepare", json=english_text)).json()
        assert english_plan["kind"] == "supplement_required", english_plan
        assert english_plan["has_index_matches"]
        quoted_instruction_word = scoped_text | {
            "request_id": "scoped-text-literal-not-intent",
            "question": "请在00:00至00:02有没有文字“WHEN”？",
        }
        literal_plan = (
            await client.post("/api/auto-chat/prepare", json=quoted_instruction_word)
        ).json()
        assert literal_plan["kind"] == "ready", literal_plan
        assert literal_plan["plan_kind"] == "direct_retrieval"
        quoted_range = text_query | {
            "request_id": "quoted-range-is-visible-text",
            "question": "找出文字“01:00-02:00”",
        }
        range_plan = (await client.post("/api/auto-chat/prepare", json=quoted_range)).json()
        assert range_plan["kind"] == "ready", range_plan
        range_answer = await client.post("/api/index-chat", json=quoted_range | {"engine": "index"})
        assert range_answer.status_code == 200, range_answer.text
        assert range_answer.json()["response"]["events"]
        assert range_answer.json()["index_search"]["coverage"]["time_scope"] is None
        answer = await client.post("/api/index-chat", json=text_query | {"engine": "index"})
        assert answer.status_code == 200 and answer.json()["response"]["events"]
        text_followup = body | {
            "request_id": "visible-text-followup",
            "question": "继续这个问题",
            "parent_request_id": "visible-text",
        }
        assert (await client.post("/api/auto-chat/prepare", json=text_followup)).json()[
            "kind"
        ] == "ready"
        text_answer = await client.post("/api/index-chat", json=text_followup | {"engine": "index"})
        assert text_answer.status_code == 200 and text_answer.json()["response"]["events"]
        assert "跨镜头" not in text_answer.json()["response"]["answer"]
        scoped = body | {
            "request_id": "scoped-parent",
            "question": "请在00:00至00:08找出人物，列出出现时间和画面。",
        }
        assert (await client.post("/api/auto-chat/prepare", json=scoped)).json()["kind"] == "ready"
        assert (
            await client.post("/api/index-chat", json=scoped | {"engine": "index"})
        ).status_code == 200
        child = body | {
            "request_id": "scoped-child",
            "question": "他出现在哪些时间？",
            "parent_request_id": "scoped-parent",
        }
        prepared = (await client.post("/api/auto-chat/prepare", json=child)).json()
        assert prepared["kind"] == "ready" and prepared["plan_kind"] == "direct_retrieval", prepared
        answer = await client.post("/api/index-chat", json=child | {"engine": "index"})
        assert answer.status_code == 200
        assert all(event["start"] < 8 for event in answer.json()["response"]["events"])
        assert processor.calls == 2
        missing = await client.post(
            "/api/auto-chat/prepare",
            json=body | {"request_id": "needs-detail", "question": "find people wearing red hat"},
        )
        plan = missing.json()["supplement_plan"]
        assert plan["kind"] == "supplement_candidates" and len(plan["windows"]) <= 2
        assert plan["knowledge_version"] == status["version"]
        assert plan["candidates_are_evidence"] is False
        gap = await client.post(
            "/api/auto-chat/prepare",
            json=body | {"request_id": "no-leads", "question": "find bicycle"},
        )
        assert gap.json()["kind"] == "ready" and gap.json()["plan_kind"] == "direct_retrieval"
        no_leads = await client.post(
            "/api/index-chat",
            json=body | {"request_id": "no-leads", "question": "find bicycle", "engine": "index"},
        )
        assert no_leads.status_code == 200
        assert no_leads.json()["response"]["status"] == "no_result"
        assert "不确定" in no_leads.json()["response"]["answer"]
        assert not no_leads.json()["response"]["events"]
        assert processor.calls == 2
        missing_body = body | {
            "request_id": "needs-detail",
            "question": "find people wearing red hat",
        }
        started = await client.post("/api/knowledge-chat/supplement", json=missing_body)
        assert started.status_code == 202, started.text
        for _ in range(500):
            supplement = (
                await client.get(f"/api/investigations/{desk['id']}/supplements/needs-detail")
            ).json()
            if supplement["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert supplement["state"] == "done", supplement
        # Completion can race the user's cancel/navigation action. Reporting
        # the terminal state must not discard the completed index version.
        completed_cancel = await client.post(
            f"/api/investigations/{desk['id']}/supplements/needs-detail/cancel"
        )
        assert completed_cancel.status_code == 202
        assert completed_cancel.json()["state"] == "done"
        assert supplement["version"] != status["version"]
        assert processor.calls == 26
        search = {
            "generation": 1,
            "question": "视频里有没有人物？",
            "answer_request_id": "existence",
        }
        historical = await client.post(f"/api/videos/{video}/occurrences/search", json=search)
        assert historical.status_code == 200, historical.text
        assert historical.json()["knowledge_version"] == status["version"]
        assert historical.json()["structured_conditions_evaluated"] is True
        assert historical.json()["verified_matches"] is False
        assert historical.json()["coverage"]["recognition_accuracy_verified"] is False
        latest = await client.post(
            f"/api/videos/{video}/occurrences/search", json=search | {"answer_request_id": None}
        )
        assert latest.status_code == 200
        assert latest.json()["knowledge_version"] == supplement["version"]
        # Candidate frames must come from the answer's immutable index version,
        # even after a later supplement resolves those same frames.
        scoped_search = search | {
            "question": "find people from 00:00 to 00:02",
            "conditions": [
                {"field": "kind", "value": "person"},
                {"field": "color", "value": "red"},
                {"field": "garment", "value": "hat"},
            ],
        }
        old_candidates = await client.post(
            f"/api/videos/{video}/occurrences/search", json=scoped_search
        )
        new_matches = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json=scoped_search | {"answer_request_id": None},
        )
        assert old_candidates.status_code == new_matches.status_code == 200
        assert old_candidates.json()["knowledge_version"] == status["version"]
        assert old_candidates.json()["total_occurrences"] == 0
        assert len(old_candidates.json()["uncertain_candidates"]) == 2
        assert new_matches.json()["knowledge_version"] == supplement["version"]
        assert new_matches.json()["total_occurrences"] == 2
        assert new_matches.json()["uncertain_candidates"] == []
        # An old answer without a recorded index version must never silently
        # resolve to the latest published version.
        with app.state.workspace.db() as conn:
            old_result = conn.execute(
                "SELECT result FROM queries WHERE id=? AND owner=?",
                ("existence", session["owner_id"]),
            ).fetchone()["result"]
            legacy_result = json.loads(old_result)
            legacy_result["index_search"].pop("knowledge_version")
            conn.execute(
                "UPDATE queries SET result=? WHERE id=? AND owner=?",
                (json.dumps(legacy_result), "existence", session["owner_id"]),
            )
        legacy = await client.post(f"/api/videos/{video}/occurrences/search", json=search)
        assert legacy.status_code == 409
        assert legacy.json()["detail"] == "index_answer_version_unavailable"
        with app.state.workspace.db() as conn:
            conn.execute(
                "UPDATE queries SET result=? WHERE id=? AND owner=?",
                (old_result, "existence", session["owner_id"]),
            )
        bad = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json=search | {"answer_request_id": "missing"},
        )
        assert bad.status_code == 409
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as stranger:
            stranger_session = (await stranger.get("/api/session")).json()
            stranger.headers["x-fs-csrf"] = stranger_session["csrf"]
            foreign = await stranger.post(f"/api/videos/{video}/occurrences/search", json=search)
            assert foreign.status_code == 404
        prepared = (await client.post("/api/auto-chat/prepare", json=missing_body)).json()
        assert prepared["kind"] == "ready", prepared
        final = await client.post("/api/index-chat", json=missing_body | {"engine": "index"})
        assert final.status_code == 200, final.text
        assert final.json()["index_search"]["knowledge_version"] == supplement["version"]
        assert final.json()["response"]["events"]
        assert final.json()["index_search"]["supplemented"] is True
        assert "合成测试数据" in final.json()["response"]["answer"]
        assert processor.calls == 26
        continued = body | {
            "request_id": "continued-detail",
            "question": "继续这个问题",
            "parent_request_id": "needs-detail",
        }
        prepared = (await client.post("/api/auto-chat/prepare", json=continued)).json()
        assert prepared["kind"] == "ready" and prepared["plan_kind"] == "direct_retrieval"
        result = await client.post("/api/index-chat", json=continued | {"engine": "index"})
        assert result.status_code == 200 and result.json()["response"]["events"]
        assert "未确认跨镜头为同一对象" in result.json()["response"]["answer"]
        assert processor.calls == 26
        processor.release.clear()
        processor.entered.clear()
        cancel_body = body | {
            "request_id": "cancel-detail",
            "question": "find people wearing yellow hat",
        }
        assert (
            await client.post("/api/knowledge-chat/supplement", json=cancel_body)
        ).status_code == 202
        for _ in range(300):
            if processor.entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert processor.entered.is_set()
        cancel = await client.post(
            f"/api/investigations/{desk['id']}/supplements/cancel-detail/cancel"
        )
        assert cancel.status_code == 202
        processor.release.set()
        for _ in range(300):
            raw = list((app.state.workspace.root / video).glob("knowledge-*/supplement-*.raw.json"))
            if len(raw) >= 25:
                break
            await asyncio.sleep(0.02)
        assert (await client.get(f"/api/videos/{video}/knowledge")).json()["version"] == supplement[
            "version"
        ]
        assert (
            await client.get(f"/api/investigations/{desk['id']}/supplements/cancel-detail")
        ).json()["state"] == "cancelled"
        assert (await client.post("/api/local-chat", json=body)).status_code == 409
        assert (await client.post("/api/cloud-chat", json=body)).status_code == 409
        assert (await client.post("/api/chat", json=body)).status_code == 409
        restored = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()
        assert len(restored["items"]) == 14
        import frameseek.ocr_candidates

        monkeypatch.setenv("FS_OCR_CANDIDATES", "1")
        monkeypatch.setattr(
            frameseek.ocr_candidates,
            "find_candidates",
            lambda *args: {"kind": "ocr_candidates", "frames": [], "verified_matches": False},
        )
        ocr_body = {"question": "请在00:00至00:02找出文字“EXIT”", "generation": 1}
        ocr = await client.post(f"/api/videos/{video}/ocr-candidates", json=ocr_body)
        assert ocr.status_code == 200 and ocr.headers["cache-control"] == "no-store"
        assert not ocr.json()["verified_matches"]
        assert (
            await client.post(
                f"/api/videos/{video}/ocr-candidates", json=ocr_body | {"generation": 2}
            )
        ).status_code == 409
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as stranger:
            stranger_session = (await stranger.get("/api/session")).json()
            stranger.headers["x-fs-csrf"] = stranger_session["csrf"]
            assert (
                await stranger.post(f"/api/videos/{video}/ocr-candidates", json=ocr_body)
            ).status_code == 404
        csrf = client.headers.pop("x-fs-csrf")
        assert (
            await client.post(f"/api/videos/{video}/ocr-candidates", json=ocr_body)
        ).status_code == 403
        client.headers["x-fs-csrf"] = csrf
        # The published index remains useful when the optional local processor
        # disappears; no legacy chat bypass or implicit model execution occurs.
        app.state.knowledge_fixture_refactor = False
        app.state.knowledge_fixture_processor = None
        monkeypatch.setenv("FS_KNOWLEDGE_LOCAL", "1")
        calls_before_reuse = processor.calls
        status = (await client.get(f"/api/videos/{video}/knowledge")).json()
        assert status["required_for_chat"] and status["state"] == "ready"
        assert status["processor_available"] is False
        reused = body | {"request_id": "processor-offline", "question": "find people"}
        prepared = (await client.post("/api/auto-chat/prepare", json=reused)).json()
        assert prepared["kind"] == "ready" and prepared["engine"] == "index"
        result = await client.post("/api/index-chat", json=reused | {"engine": "index"})
        assert result.status_code == 200 and result.json()["response"]["events"]
        assert (await client.post("/api/chat", json=reused)).status_code == 409
        needs_processor = body | {
            "request_id": "processor-offline-detail",
            "question": "find people wearing yellow hat",
        }
        unavailable = (await client.post("/api/auto-chat/prepare", json=needs_processor)).json()
        assert unavailable["plan_kind"] == "processor_unavailable"
        assert "本机补查暂不可用" in unavailable["message"]
        assert unavailable["model_requests_sent"] == 0
        assert processor.calls == calls_before_reuse
        with app.state.agent.ledger._connect() as connection:
            assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        processor.release.set()
        await client.aclose()
