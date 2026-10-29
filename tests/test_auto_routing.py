import pytest

from frameseek.auto_routing import choose_engine, sufficient_index


def complete():
    return {
        "kind": "indexed_occurrences",
        "structured_conditions_evaluated": True,
        "verified_matches": False,
        "coverage": {"all_source_frames_reviewed": True, "exhaustive_under_index": True},
    }


def test_only_complete_structured_conditions_can_use_index_automatically():
    good = complete()
    assert sufficient_index(good)
    for bad in [
        {"kind": "visual_candidates", "coverage": good["coverage"]},
        good | {"structured_conditions_evaluated": False},
        good | {"coverage": {"all_source_frames_reviewed": False}},
        good | {"aggregation": {"exact_under_index": False}},
    ]:
        assert not sufficient_index(bad)
    assert choose_engine("找白衣服的人", good, local_ready=True, cloud_ready=True)[0] == "index"


def test_no_recognition_of_speech_from_pictures_and_no_paid_route_before_local():
    assert choose_engine("找白衣服的人", {}, local_ready=True, cloud_ready=True)[0] == "local"
    assert choose_engine("find person", {}, local_ready=False, cloud_ready=True)[0] == "cloud"
    assert choose_engine("find person", {}, local_ready=False, cloud_ready=False)[0] == "model"
    assert (
        choose_engine("解说说了什么？", complete(), local_ready=True, cloud_ready=True)[0]
        == "model"
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_auto_prepare_owned_readonly_local_fallback_and_budget_block(tmp_path, monkeypatch):
    from test_index_chat import setup_index_page

    import frameseek.routes as routes
    from frameseek.cloud_vision import CloudVision

    app, client, video, desk, session, index = await setup_index_page(tmp_path, monkeypatch)
    body = {
        "investigation_id": desk["id"],
        "request_id": "auto-check",
        "generation": 0,
        "question": "找出穿白色球衣的人，列出所有出现时间",
    }
    monkeypatch.setattr(routes, "local_configured", lambda: None)
    monkeypatch.setattr(CloudVision, "configured", lambda self: False)
    try:
        with app.state.workspace.db() as c:
            before = c.execute("select count(*) from queries").fetchone()[0]
        planned = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert planned["engine"] == "index"
        monkeypatch.setattr(routes, "local_configured", lambda: ("offline", "model"))
        local = (
            await client.post("/api/auto-chat/prepare", json=body | {"question": "描述未知动作"})
        ).json()
        assert local["kind"] == "ready" and local["engine"] == "local"
        monkeypatch.setattr(routes, "local_configured", lambda: None)
        monkeypatch.setattr(CloudVision, "configured", lambda self: True)
        monkeypatch.setattr(
            CloudVision, "prepare", lambda *args: {"kind": "budget_blocked", "message": "denied"}
        )
        denied = (
            await client.post("/api/auto-chat/prepare", json=body | {"question": "描述未知动作"})
        ).json()
        assert denied["kind"] == "budget_blocked" and denied["engine"] == "cloud"
        assert denied["model_requests_sent"] == 0
        assert (
            await client.post("/api/auto-chat/prepare", json=body, headers={"x-fs-csrf": "bad"})
        ).status_code == 403
        assert (
            await client.post("/api/auto-chat/prepare", json=body | {"generation": 9})
        ).status_code == 409
        with app.state.workspace.db() as c:
            assert c.execute("select count(*) from queries").fetchone()[0] == before
        completed = await client.post("/api/index-chat", json=body | {"engine": "index"})
        assert completed.status_code == 200
        repeat = (await client.post("/api/auto-chat/prepare", json=body)).json()
        assert repeat["kind"] == "request_exists" and repeat["model_requests_sent"] == 0
        # A model-backed parent is valid for Auto even though index-only repeat
        # parsing deliberately refuses it. No model is run by this fixture.
        import json

        saved = completed.json() | {"engine": "local"}
        with app.state.workspace.db() as c:
            c.execute(
                "UPDATE queries SET result=? WHERE id=?", (json.dumps(saved), body["request_id"])
            )
        monkeypatch.setattr(routes, "local_configured", lambda: ("offline", "model"))
        followup = (
            await client.post(
                "/api/auto-chat/prepare",
                json=body
                | {
                    "request_id": "model-followup",
                    "question": "继续",
                    "parent_request_id": body["request_id"],
                },
            )
        ).json()
        assert followup["kind"] == "ready" and followup["engine"] == "local"
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as stranger:
            other = (await stranger.get("/api/session")).json()
            stranger.headers["x-fs-csrf"] = other["csrf"]
            assert (await stranger.post("/api/auto-chat/prepare", json=body)).status_code == 404
    finally:
        await client.aclose()
