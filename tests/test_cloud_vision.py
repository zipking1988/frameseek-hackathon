"""Cloud compiler wiring uses isolated budgets and mock HTTP, never paid calls."""

import json
from types import SimpleNamespace

import httpx
import pytest
from test_agent import approve
from test_index_chat import setup_index_page
from test_tokenfactory import configuration

from frameseek.agent import AgentError, Provider
from frameseek.attempts import AttemptLedger
from frameseek.cloud_vision import CloudClient, CloudVision, validate_shape
from frameseek.local_vision import packet_schema


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_cloud_transport_ledger_cache_origin_and_foreign_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    ledger = AttemptLedger(tmp_path / "attempts.sqlite3")
    approve(ledger, "owner", calls=5, money=5)
    calls = []
    result = {
        "summary": "test",
        "observations": [{"description": "test", "frames": [0], "uncertain": False}],
        "uncertainties": [],
    }

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.tokenfactory.nebius.com/v1/chat/completions"
        assert b"data:image/jpeg;base64," in request.content
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]
            },
        )

    provider = Provider(configuration(), ledger, lambda: None, httpx.MockTransport(handler))
    provider.cache_dir = tmp_path / "model-results"
    directory = tmp_path / "source"
    (directory / "frames").mkdir(parents=True)
    image = directory / "frames/a.jpg"
    image.write_bytes(b"\xff\xd8\xfftest-only\xff\xd9")
    client = CloudClient(provider, "owner", "generation-1", directory, lambda: True)
    for _ in range(2):
        generated = client.infer("test prompt", [image], schema=packet_schema(1))
        assert json.loads(generated["text"]) == result
    assert len(calls) == 1
    with ledger._connect() as c:
        assert c.execute("select used_calls from budgets where scope='owner'").fetchone()[0] == 1
    with pytest.raises(AgentError, match="foreign_visual_frame"):
        client.infer("test", [tmp_path / "foreign.jpg"])
    assert len(calls) == 1
    # Configuration/generation identity changes cannot reuse the previous paid receipt.
    other = CloudClient(provider, "owner", "generation-2", directory, lambda: True)
    other.infer("test prompt", [image], schema=packet_schema(1))
    assert len(calls) == 2


def test_cloud_shape_rejects_invented_indices_and_wrong_types():
    schema = packet_schema(1)
    value = {
        "summary": "test",
        "observations": [{"description": "test", "frames": [7], "uncertain": False}],
        "uncertainties": [],
    }
    with pytest.raises(AgentError, match="schema_invalid"):
        validate_shape(value, schema)
    value["observations"][0]["frames"] = [True]
    with pytest.raises(AgentError):
        validate_shape(value, schema)


def test_cloud_readonly_plan_cached_ingest_and_exhausted_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_CLOUD_VISION", "1")
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    runtime = configuration()
    ledger = AttemptLedger(tmp_path / "attempts.sqlite3")
    approve(ledger, "owner", calls=100, money=100)
    directory = tmp_path / "video"
    directory.mkdir()
    (directory / "original").write_bytes(b"test-only original")
    agent = SimpleNamespace(
        runtime=runtime,
        settings=SimpleNamespace(mode="real"),
        ledger=ledger,
        ws=SimpleNamespace(root=tmp_path),
    )
    cloud = CloudVision(agent)
    source = {"id": "video", "generation": 1, "metadata": '{"duration":16}'}
    first = cloud.prepare(source, "owner", "describe the scene")
    assert first["kind"] == "ready" and first["estimate"]["new_index_calls"] == 2
    identity, cache = cloud.location(source)
    cache.mkdir()
    (cache / "window-0000.json").write_text(json.dumps({"identity": identity}))
    second = cloud.prepare(source, "owner", "describe the scene")
    assert second["estimate"]["new_index_calls"] == 1
    assert second["estimate"]["maximum_calls"] == first["estimate"]["maximum_calls"] - 1
    with ledger._connect() as c:
        c.execute("update budgets set used_calls=call_cap")
    blocked = cloud.prepare(source, "owner", "找出白色衣服的人，列出出现时间")
    assert blocked["kind"] == "budget_blocked" and blocked["estimate"]["maximum_calls"] >= 5
    with ledger._connect() as c:
        assert c.execute("select count(*) from attempts").fetchone()[0] == 0
    monkeypatch.delenv("FS_CLOUD_VISION")
    assert cloud.prepare(source, "owner", "test")["kind"] == "capability_blocked"


@pytest.mark.anyio
async def test_cloud_route_direct_post_cannot_bypass_admission(tmp_path, monkeypatch):
    from frameseek.cloud_vision import CloudVision

    monkeypatch.setattr(CloudVision, "configured", lambda self: True)
    monkeypatch.setattr(
        CloudVision,
        "prepare",
        lambda self, *args: {"kind": "budget_blocked", "message": "test-only exhausted budget"},
    )
    app, client, video, desk, session, index = await setup_index_page(tmp_path, monkeypatch)
    body = {
        "investigation_id": desk["id"],
        "request_id": "cloud-blocked",
        "generation": 0,
        "question": "test",
    }
    try:
        assert (await client.post("/api/cloud-chat/prepare", json=body)).json()[
            "kind"
        ] == "budget_blocked"
        assert (await client.post("/api/cloud-chat", json=body)).json()["kind"] == "budget_blocked"
        assert (
            await client.post("/api/cloud-chat", json=body, headers={"x-fs-csrf": "bad"})
        ).status_code == 403
        with app.state.workspace.db() as c:
            assert c.execute("select count(*) from queries").fetchone()[0] == 0
    finally:
        await client.aclose()


def test_cloud_negative_batch_does_not_trigger_automatic_single_image_fanout(tmp_path):
    from frameseek.local_vision import refine_uncovered_frames

    class Client:
        confirm_batch_negatives = False
        calls = 0

        def infer(self, *args, **kwargs):
            self.calls += 1
            return {"text": '{"matching_frames":[]}', "elapsed": 0}

    client = Client()
    row = {
        "window": 0,
        "frames": [{"frame_id": "f0", "file": "f0.jpg", "t": 0}],
        "analysis": {"observations": []},
    }
    result = refine_uncovered_frames(
        client,
        tmp_path,
        "test",
        tmp_path,
        "find white shirt",
        None,
        [row],
        {"claims": [], "uncertainties": []},
        lambda message: None,
        lambda: True,
    )
    assert client.calls == 1 and result["claims"] == []
    assert "matches may have been missed" in result["uncertainties"][0]


def test_cloud_model_context_is_bounded_without_rewriting_original_anchor():
    context = {
        "question": "问题" * 4000,
        "answer": "回答" * 4000,
        "events": [
            {"start": n, "end": n + 1, "evidence": ["private uri"] * 128} for n in range(100)
        ],
    }
    bounded = CloudClient.context_for_model(context)
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) < 2000
    assert len(context["events"]) == 100 and "private uri" not in str(bounded)
