"""Owned browser/API flow with isolated ledgers and offline-only mock vision."""

import json

import httpx
import pytest
from test_agent import setup_real
from test_tokenfactory import configuration, handler


@pytest.fixture
def anyio_backend():
    return "asyncio"


def visual_handler(calls, mode):
    original = handler(calls)

    def respond(request):
        payload = json.loads(request.content)
        messages = payload.get("messages", [])
        content = messages[1].get("content") if len(messages) > 1 else None
        if isinstance(content, list) and content[0].get("text", "").startswith(
            '{"task": "video_ctrl_f"'
        ):
            calls.append((str(request.url), payload))
            prompt = json.loads(content[0]["text"])
            assert prompt["question"]
            frames = [json.loads(item["text"]) for item in content[1:] if item["type"] == "text"]
            criteria = [
                {"id": "one", "description": "condition from arbitrary question"},
                {"id": "two", "description": "second condition on same subject"},
            ]
            matches = [
                {
                    "frame_id": f["frame_id"],
                    "subject_id": "subject1",
                    "statement": "离线合成测试匹配。",
                    "checks": [
                        {"criterion_id": key, "subject_id": "subject1", "verdict": "yes"}
                        for key in ("one", "two")
                    ],
                }
                for f in frames[:2]
            ]
            if mode == "foreign":
                matches[0]["frame_id"] = "other_source"
            if mode == "different_subjects":
                matches[0]["checks"][1]["subject_id"] = "subject2"
            if mode == "negative":
                matches = []
            result = {
                "criteria": criteria,
                "matches": matches,
                "no_match": mode == "negative",
                "unresolved": [],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(result)}}
                    ]
                },
            )
        return original(request)

    return respond


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["valid", "negative", "foreign", "different_subjects"])
async def test_open_question_uses_one_verified_call_and_local_index_needs_none(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    app, client, video, desk = await setup_real(
        tmp_path,
        monkeypatch,
        calls,
        runtime_factory=configuration,
        response_handler=visual_handler(calls, mode),
    )
    try:
        ledger = app.state.agent.ledger
        ledger.visual_search = True
        ledger.direct_observations = True
        ledger.allow_repairs = False
        with ledger._connect() as c:
            c.execute("UPDATE budgets SET call_cap=used_calls+1,money_cap=used_money+1")
            before_ledger = [
                tuple(row) for row in c.execute("SELECT * FROM budgets ORDER BY scope")
            ]
        before = len(calls)
        source = (await client.get(f"/api/videos/{video}")).json()
        search = {
            "question": "列出戴红色帽子同时穿白色衣服的人出现的时间",
            "generation": source["generation"],
        }
        result = await client.post(f"/api/videos/{video}/search-index", json=search)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        candidate = result.json()
        assert candidate["verified_matches"] is False
        assert candidate["model_requests_sent"] == 0
        assert candidate["frames"]
        # Full-index browsing is a separate no-call path, never top-eight proof.
        exhaustive = await client.post(
            f"/api/videos/{video}/search-index",
            json=search
            | {"question": candidate["frames"][0]["caption"], "all_saved": True, "limit": 1},
        )
        assert exhaustive.status_code == 200
        page = exhaustive.json()
        assert page["total_candidates"] >= 1 and len(page["frames"]) == 1
        assert page["all_source_occurrences_verified"] is False
        assert page["model_requests_sent"] == 0
        if page["next_offset"] is not None:
            continuation = await client.post(
                f"/api/videos/{video}/search-index",
                json=search
                | {
                    "question": candidate["frames"][0]["caption"],
                    "all_saved": True,
                    "offset": page["next_offset"],
                    "limit": 1,
                    "snapshot": page["snapshot"],
                },
            )
            assert continuation.status_code == 200
            stale_page = await client.post(
                f"/api/videos/{video}/search-index",
                json=search
                | {"all_saved": True, "offset": page["next_offset"], "snapshot": page["snapshot"]},
            )
            assert stale_page.status_code == 409
        assert len(calls) == before
        with ledger._connect() as c:
            assert [
                tuple(row) for row in c.execute("SELECT * FROM budgets ORDER BY scope")
            ] == before_ledger
        stale = await client.post(
            f"/api/videos/{video}/search-index", json=search | {"generation": 99}
        )
        assert stale.status_code == 409
        denied = await client.post(
            f"/api/videos/{video}/search-index", json=search, headers={"x-fs-csrf": "wrong"}
        )
        assert denied.status_code == 403
        body = {
            "investigation_id": desk["id"],
            "generation": 0,
            "request_id": "ctrl_f",
            "question": search["question"],
        }
        assert (await client.post("/api/chat/prepare", json=body)).json()["kind"] == "ready"
        result = await client.post("/api/chat", json=body)
        response = result.json()["response"]
        assert len(calls) == before + 1
        if mode == "valid":
            assert response["status"] == "partial" and len(response["events"]) == 2
            for event in response["events"]:
                assert event["end"] - event["start"] <= 0.0011
                assert len(event["evidence"]) == 1
                assert (await client.get(event["evidence"][0]["uri"])).status_code == 200
        elif mode == "negative":
            assert response["status"] == "no_result" and not response["events"]
            assert "does not establish absence from the full video" in response["answer"]
        else:
            assert response["status"] == "error" and not response["events"]
        assert (await client.post("/api/chat", json=body)).json() == result.json()
        assert len(calls) == before + 1  # Idempotent recovery is not another paid stage.
        with ledger._connect() as c:
            assert all(row[0] == 0 for row in c.execute("SELECT call_cap-used_calls FROM budgets"))
        # Existing index search remains usable when model requests are exhausted.
        assert (
            await client.post(f"/api/videos/{video}/search-index", json=search)
        ).status_code == 200
        assert len(calls) == before + 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as other:
            s = (await other.get("/api/session")).json()
            other.headers["x-fs-csrf"] = s["csrf"]
            assert (
                await other.post(f"/api/videos/{video}/search-index", json=search)
            ).status_code == 404
    finally:
        await client.aclose()
