"""Historical model contexts stay executable under the unified index contract."""

import json

import pytest
from test_index_chat import setup_index_page
from test_knowledge_compiler import infer

from frameseek.index_service import source_digest
from frameseek.knowledge_compiler import compile_pending
from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("historical_engine", ["local", "cloud", "model"])
async def test_unified_followup_prepare_execute_and_supplement_share_context(
    tmp_path, monkeypatch, historical_engine
):
    app, client, video, desk, session, _ = await setup_index_page(tmp_path, monkeypatch)
    ws = app.state.workspace
    body = {
        "investigation_id": desk["id"],
        "request_id": "historical-parent",
        "generation": 0,
        "question": "有没有人",
    }
    try:
        parent = await client.post("/api/index-chat", json=body | {"engine": "index"})
        assert parent.status_code == 200, parent.text
        saved = parent.json() | {"engine": historical_engine}
        saved.pop("index_search", None)
        with ws.db() as c:
            c.execute(
                "UPDATE queries SET result=? WHERE id=?", (json.dumps(saved), body["request_id"])
            )
        source = ws.owned("videos", video, session["owner_id"])
        metadata = json.loads(source["metadata"])
        directory = ws.root / video
        store = KnowledgeStore(
            directory,
            VideoKnowledge(
                video=video,
                generation=source["generation"],
                duration=metadata["duration"],
                source_fingerprint=source_digest(directory),
                processor_revision="synthetic context regression",
                extraction_config="8-second fixture samples",
                scene_boundaries=metadata["scene_boundaries"],
            ),
        )
        version = compile_pending(store, metadata, infer, lambda: True)
        with ws.db() as c:
            c.execute(
                "UPDATE knowledge_jobs SET state='ready',version=?,cache_key=? "
                "WHERE video=? AND generation=?",
                (version, store.key, video, source["generation"]),
            )
        app.state.knowledge_fixture_refactor = True
        app.state.knowledge_fixture_processor = None
        follow = body | {
            "request_id": "historical-followup",
            "question": "继续",
            "parent_request_id": body["request_id"],
        }
        prepared = await client.post("/api/auto-chat/prepare", json=follow)
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["kind"] == "ready", prepared.text
        assert prepared.json()["engine"] == "index"
        ready = await client.post("/api/index-chat/prepare", json=follow | {"engine": "index"})
        assert ready.status_code == 200, ready.text
        result = await client.post("/api/index-chat", json=follow | {"engine": "index"})
        assert result.status_code == 200, result.text
        assert result.json()["index_search"]["resolved_question"] == body["question"]
        assert result.json()["response"]["events"]
        repeat = await client.post("/api/index-chat", json=follow | {"engine": "index"})
        assert repeat.json() == result.json()

        import frameseek.knowledge_planning as planning
        from frameseek.knowledge_jobs import FixtureKnowledgeJobs

        captured = []

        def plan(index, question, revision):
            assert question == body["question"]
            assert revision == version
            return {"kind": "supplement_candidates", "knowledge_version": revision}

        def start(self, current_source, current_desk, owner, request, question, planned):
            assert owner == session["owner_id"]
            captured.append(question)
            return {"state": "queued", "model_requests_sent": 0}

        monkeypatch.setattr(planning, "plan_supplement", plan)
        monkeypatch.setattr(FixtureKnowledgeJobs, "start_supplement", start)
        supplement = await client.post(
            "/api/knowledge-chat/supplement", json=follow | {"request_id": "historical-supplement"}
        )
        assert supplement.status_code == 202, supplement.text
        assert captured == [body["question"]]

        if historical_engine == "model":
            legacy = dict(saved)
            legacy.pop("source_generation")
            with ws.db() as c:
                c.execute(
                    "UPDATE queries SET result=? WHERE id=?",
                    (json.dumps(legacy), body["request_id"]),
                )
            initial = await client.post(
                "/api/index-chat/prepare",
                json=follow | {"request_id": "legacy-first-generation", "engine": "index"},
            )
            assert initial.status_code == 200 and initial.json()["kind"] == "ready", initial.text
            with ws.db() as c:
                c.execute("UPDATE videos SET generation=generation+1 WHERE id=?", (video,))
            app.state.knowledge_fixture_refactor = False
            ignored = await client.post(
                "/api/index-chat/prepare",
                json=follow
                | {
                    "request_id": "legacy-disabled-context",
                    "engine": "index",
                    "question": "白衣服的人在哪里？",
                },
            )
            assert (
                ignored.status_code == 409
                and ignored.json()["detail"] == "stale_context_generation"
            )
            app.state.knowledge_fixture_refactor = True
            for endpoint, extra in [
                ("/api/auto-chat/prepare", {}),
                ("/api/index-chat/prepare", {"engine": "index"}),
                ("/api/knowledge-chat/supplement", {}),
            ]:
                blocked = await client.post(
                    endpoint,
                    json=follow | {"request_id": "legacy-stale-" + endpoint.split("/")[-1]} | extra,
                )
                assert (
                    blocked.status_code == 409
                    and blocked.json()["detail"] == "stale_context_generation"
                )
            with ws.db() as c:
                c.execute(
                    "UPDATE videos SET generation=? WHERE id=?", (source["generation"], video)
                )
                c.execute(
                    "UPDATE queries SET result=? WHERE id=?",
                    (json.dumps(saved), body["request_id"]),
                )

        # Source and desk generation checks still guard the shared resolver.
        with ws.db() as c:
            stale = saved | {"source_generation": source["generation"] + 1}
            c.execute(
                "UPDATE queries SET result=? WHERE id=?", (json.dumps(stale), body["request_id"])
            )
        for endpoint, extra in [
            ("/api/auto-chat/prepare", {}),
            ("/api/index-chat", {"engine": "index"}),
            ("/api/knowledge-chat/supplement", {}),
        ]:
            blocked = await client.post(
                endpoint, json=follow | {"request_id": "stale-" + endpoint.split("/")[-1]} | extra
            )
            assert blocked.status_code == 409, blocked.text
        with ws.db() as c:
            c.execute(
                "UPDATE queries SET result=? WHERE id=?", (json.dumps(saved), body["request_id"])
            )
            assert (
                json.loads(
                    c.execute(
                        "SELECT result FROM queries WHERE id=?", (body["request_id"],)
                    ).fetchone()[0]
                )
                == saved
            )
        assert (await client.get("/health")).json()["model_requests_sent"] == 0
    finally:
        await client.aclose()
