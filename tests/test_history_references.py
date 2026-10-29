"""History keeps owned query references instead of silently evicting round13."""

import copy
import json

import pytest
from test_index_chat import setup_index_page


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_compact_owned_history_roundtrip_more_than_twelve_and_boundaries(
    tmp_path, monkeypatch
):
    app, client, video, desk, session, _ = await setup_index_page(tmp_path, monkeypatch)
    try:
        assert session["query_reference_snapshots"] is True
        results = []
        for i in range(14):
            reply = await client.post(
                "/api/index-chat",
                json={
                    "investigation_id": desk["id"],
                    "request_id": f"history-{i}",
                    "generation": 0,
                    "question": "white jersey",
                    "engine": "index",
                },
            )
            assert reply.status_code == 200
            results.append(reply.json())
        state = {
            "title": "history test",
            "records": [
                {
                    "id": f"task-{i + 1}",
                    "query": {"id": result["id"], **({"parentTurn": "task-1"} if i == 1 else {})},
                }
                for i, result in enumerate(results)
            ],
            "pins": [
                {
                    "id": "finding-1",
                    "kind": "evidence",
                    "turnId": "task-1",
                    "query": {"id": results[0]["id"]},
                    "frame": results[0]["response"]["events"][0]["evidence"][0],
                }
            ],
            "ui": {"draft": "keep this unsent draft"},
        }
        route = f"/api/investigations/{desk['id']}"
        saved = await client.put(route, json={"revision": 0, "state": state})
        assert saved.status_code == 200, saved.text
        restored = (await client.get(route)).json()
        assert len(restored["state"]["records"]) == 14
        assert restored["state"]["records"][0]["query"]["response"] == results[0]["response"]
        assert restored["state"]["records"][1]["query"]["parentTurn"] == "task-1"
        assert restored["state"]["ui"]["draft"] == "keep this unsent draft"
        with app.state.workspace.db() as c:
            stored = json.loads(
                c.execute("SELECT state FROM investigations WHERE id=?", (desk["id"],)).fetchone()[
                    0
                ]
            )
        assert all("response" not in r["query"] for r in stored["records"])
        assert "response" not in stored["pins"][0]["query"]

        tampered = copy.deepcopy(restored["state"])
        tampered["records"][0]["query"]["response"]["answer"] = "forged"
        assert (await client.put(route, json={"revision": 1, "state": tampered})).status_code == 422
        other = (await client.post("/api/investigations", json={"video_id": video})).json()
        foreign = (
            await client.post(
                "/api/index-chat",
                json={
                    "investigation_id": other["id"],
                    "request_id": "foreign-desk-history",
                    "generation": 0,
                    "question": "white jersey",
                    "engine": "index",
                },
            )
        ).json()
        bad = copy.deepcopy(state)
        bad["records"][0]["query"] = {"id": foreign["id"]}
        assert (await client.put(route, json={"revision": 1, "state": bad})).status_code == 422
        too_large = copy.deepcopy(state)
        too_large["note"] = "x" * (1024**2 + 1)
        assert (
            await client.put(route, json={"revision": 1, "state": too_large})
        ).status_code == 413
        assert (await client.get(route)).json()["revision"] == 1
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        await client.aclose()
