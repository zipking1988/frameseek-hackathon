"""An upload's playback preflight is not a completed knowledge index."""

import httpx
import pytest
from test_index_chat import setup_index_page
from test_knowledge_compiler import infer

from frameseek.index_service import source_digest
from frameseek.knowledge_compiler import compile_pending
from frameseek.occurrences import search_occurrences
from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("endpoint", ["detail", "list"])
async def test_video_reads_preflight_and_metadata_from_one_snapshot(
    tmp_path, monkeypatch, endpoint
):
    import json
    from contextlib import contextmanager

    app, client, video, _, _, _ = await setup_index_page(tmp_path, monkeypatch)
    ws = app.state.workspace
    original_db = ws.db
    try:
        with original_db() as connection:
            connection.execute("UPDATE videos SET metadata='{}' WHERE id=?", (video,))
            connection.execute("UPDATE jobs SET state='probing' WHERE video=?", (video,))
        changed = False

        class InterleavedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                nonlocal changed
                result = self.connection.execute(sql, parameters)
                if not changed and sql in {
                    "SELECT * FROM videos WHERE id=? AND owner=?",
                    "SELECT * FROM videos WHERE owner=? AND deleted=0 AND expires>?",
                }:
                    changed = True
                    with original_db() as other:
                        other.execute(
                            "UPDATE videos SET metadata=? WHERE id=?",
                            (json.dumps({"duration": 12}), video),
                        )
                        other.execute("UPDATE jobs SET state='ready' WHERE video=?", (video,))
                return result

        @contextmanager
        def interleaved_db():
            with original_db() as connection:
                yield InterleavedConnection(connection)

        monkeypatch.setattr(ws, "db", interleaved_db)
        url = f"/api/videos/{video}" if endpoint == "detail" else "/api/videos"
        first = await client.get(url)
        assert first.status_code == 200, first.text
        first_item = first.json() if endpoint == "detail" else first.json()["items"][0]
        assert first_item["metadata"] == {}
        assert first_item["job"]["state"] == "probing"
        second = await client.get(url)
        second_item = second.json() if endpoint == "detail" else second.json()["items"][0]
        assert second_item["metadata"]["duration"] == 12
        assert second_item["job"]["state"] == "ready"
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_status_distinguishes_preflight_from_index_and_checks_owner(tmp_path, monkeypatch):
    app, client, video, _, _, _ = await setup_index_page(tmp_path, monkeypatch)
    try:
        detail = (await client.get(f"/api/videos/{video}")).json()
        status = (await client.get(f"/api/videos/{video}/knowledge")).json()
        assert detail["job"]["state"] == "ready"
        assert detail["knowledge_status"] == status
        assert status["state"] == "waiting_processor"
        assert status["completed_segments"] == 0
        assert status["total_segments"] == 2
        assert status["version"] is None
        assert status["coverage"] == "not_indexed"
        assert status["capabilities"]["speech_transcription"] == "not_enabled"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as stranger:
            await stranger.get("/api/session")
            assert (await stranger.get(f"/api/videos/{video}/knowledge")).status_code == 404
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_status_does_not_advertise_disabled_local_processor(tmp_path, monkeypatch):
    app, client, video, _, _, _ = await setup_index_page(tmp_path, monkeypatch)
    try:
        import frameseek.routes as routes

        app.state.knowledge_fixture_refactor = True
        app.state.knowledge_fixture_processor = None
        monkeypatch.setattr(routes, "local_configured", lambda: True)
        monkeypatch.delenv("FS_KNOWLEDGE_LOCAL", raising=False)
        status = (await client.get(f"/api/videos/{video}/knowledge")).json()
        assert status["required_for_chat"]
        assert status["processor_available"] is False
        monkeypatch.setenv("FS_KNOWLEDGE_LOCAL", "1")
        status = (await client.get(f"/api/videos/{video}/knowledge")).json()
        assert status["processor_available"] is True
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_real_decoder_and_synthetic_extraction_are_reusable(tmp_path, monkeypatch):
    app, client, video, _, session, _ = await setup_index_page(tmp_path, monkeypatch)
    try:
        source = app.state.workspace.owned("videos", video, session["owner_id"])
        directory = app.state.workspace.root / video
        detail = (await client.get(f"/api/videos/{video}")).json()
        metadata = detail["metadata"]
        state = VideoKnowledge(
            video=video,
            generation=source["generation"],
            duration=metadata["duration"],
            source_fingerprint=source_digest(directory),
            processor_revision="synthetic fixture, not a model",
            extraction_config="8-second samples",
            scene_boundaries=metadata["scene_boundaries"],
        )
        store = KnowledgeStore(directory, state)
        calls = []

        def synthetic(*args, **kwargs):
            calls.append(1)
            return infer(*args, **kwargs)

        version = compile_pending(store, metadata, synthetic, lambda: True)
        assert len(calls) == 2
        index = store.read_version(version).publishable_index()
        for _ in range(2):
            result = search_occurrences(index, [{"field": "kind", "value": "person"}])
            assert result["occurrences"]
            assert not result["coverage"]["all_source_frames_reviewed"]
        assert len(calls) == 2
    finally:
        await client.aclose()
