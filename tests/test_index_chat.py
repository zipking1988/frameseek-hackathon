"""The full page/API engine uses annotated synthetic evidence, never model calls."""

import asyncio
import shutil
import subprocess

import httpx
import pytest

from frameseek.api import create_app
from frameseek.config import Settings
from frameseek.media import frames, probe
from frameseek.occurrences import checkpoint, plan_source, save_index


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def setup_index_page(tmp_path, monkeypatch, event_fixture=False, partial_fixture=False):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "workspace"))
    for key in (
        "FS_SHARED_LEDGER_SSH",
        "FS_SHARED_LEDGER_LOCAL_OWNER",
        "FS_PUBLIC_ORIGIN",
        "FS_RUNTIME_FILE",
        "FS_VISUAL_SEARCH",
    ):
        monkeypatch.delenv(key, raising=False)
    # No runtime, provider, keys or mock model transport is installed.
    app = create_app(Settings(mode="fixture"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    session = (await client.get("/api/session")).json()
    client.headers["x-fs-csrf"] = session["csrf"]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    clip = tmp_path / "source.mp4"
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
    upload = await client.post(
        "/api/videos", content=clip.read_bytes(), headers={"content-type": "video/mp4"}
    )
    video = upload.json()["id"]
    for _ in range(100):
        state = (await client.get(f"/api/videos/{video}")).json()
        if state["job"]["state"] not in ("queued", "probing"):
            break
        await asyncio.sleep(0.02)
    ws = app.state.workspace
    source = ws.owned("videos", video, session["owner_id"])
    directory = ws.root / video
    metadata = probe(directory)
    index = plan_source(
        directory, video, source["generation"], metadata, "SYNTHETIC MANUAL ANNOTATIONS / NO MODEL"
    )
    # Decode every real2FPS frame in one-second bounded windows. Hand-authored
    # annotations exercise retrieval semantics only, not recognition accuracy.
    catalog = index.model_dump()
    for i, frame in enumerate(catalog["frames"]):
        decoded = frames(
            directory, video, metadata, {"start": frame["t"], "end": frame["end"]}, 1, f"qa{i}"
        )[0]
        assert decoded["t"] == frame["t"]
        frame.update({k: decoded[k] for k in ("file", "frame_id", "uri")})
    from frameseek.occurrences import ObservationIndex

    if event_fixture:
        catalog["event_aliases"] = {"搬运": "lift", "开门": "door_open"}
        catalog["event_rules"] = [
            {
                "kind": "door_open",
                "before": [
                    {"field": "kind", "value": "door"},
                    {"field": "state", "value": "closed"},
                ],
                "after": [{"field": "kind", "value": "door"}, {"field": "state", "value": "open"}],
            }
        ]
    index = ObservationIndex.model_validate(catalog)
    annotations = []
    for i, frame in enumerate(index.frames):
        positive = i < 4 or 8 <= i < 12 or i >= 20
        facts = {
            "kind": ["person"],
            "color": ["white" if positive else "blue"],
            # The human-authored fixture explicitly binds the colour to the item.
            "garment": ["white jersey" if positive else "blue jersey"],
        }
        annotations.append(
            {
                "frame_id": frame.frame_id,
                "inventory_complete": True,
                "subjects": [{"id": "frame_local", "facts": facts}],
            }
        )
    if event_fixture:
        for i, row in enumerate(annotations):
            row["event_inventory_complete"] = True
            row["subjects"] = [
                {
                    "id": "door",
                    "track_id": "door-track",
                    "entity_id": "door-entity",
                    "facts": {
                        "kind": ["door"],
                        "state": ["open" if i in (4, 5, 6, 16, 17, 18) else "closed"],
                    },
                }
            ]
            if i in (0, 1, 8, 9, 20, 21):
                row["subjects"].append(
                    {
                        "id": "parcel",
                        "track_id": "track-one",
                        "entity_id": "entity-one",
                        "facts": {"kind": ["parcel"], "event": ["lift"]},
                        "event_keys": {"lift": "first" if i < 10 else "second"},
                    }
                )
    if partial_fixture:
        for row in annotations[:2]:
            row["subjects"] = [
                {
                    "id": "person",
                    "facts": {"kind": ["person"], "garment": ["jersey"], "color": ["white"]},
                }
            ]
    for offset in range(0, len(annotations), 8):
        index = checkpoint(index, annotations[offset : offset + 8], index.snapshot())
    save_index(directory / f"observations-g{source['generation']}.json", index)
    with ws.db() as c:
        c.execute("UPDATE jobs SET state='ready' WHERE video=?", (video,))
    desk = (
        await client.post(
            "/api/investigations", json={"video_id": video, "title": "离线全片索引验收 · 合成标注"}
        )
    ).json()
    return app, client, video, desk, session, index


@pytest.mark.anyio
async def test_uncertain_candidates_are_owned_replayable_and_never_answer_evidence(
    tmp_path, monkeypatch
):
    app, client, video, desk, _, index = await setup_index_page(
        tmp_path, monkeypatch, partial_fixture=True
    )
    try:
        question = "请在00:00至00:01找出穿白色球衣的人"
        found = await client.post(
            f"/api/videos/{video}/occurrences/search", json={"question": question, "generation": 1}
        )
        assert found.status_code == 200, found.text
        result = found.json()
        assert result["total_occurrences"] == 0
        assert result["uncertain_candidate_total"] == 2
        for frame in result["uncertain_candidates"]:
            assert (await client.get(frame["uri"])).status_code == 200
        jump_body = {
            "question": question,
            "generation": 1,
            "snapshot": result["snapshot"],
            "candidate_time": 0.5,
        }
        jumped = await client.post(f"/api/videos/{video}/occurrences/search", json=jump_body)
        assert jumped.status_code == 200
        assert [frame["t"] for frame in jumped.json()["uncertain_candidates"]] == [0.5]
        mismatched = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json=jump_body | {"question": "请在00:00至00:01找出穿蓝色球衣的人"},
        )
        assert mismatched.status_code == 409
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as stranger:
            stranger_session = (await stranger.get("/api/session")).json()
            stranger.headers["x-fs-csrf"] = stranger_session["csrf"]
            assert (
                await stranger.post(f"/api/videos/{video}/occurrences/search", json=jump_body)
            ).status_code == 404
            assert (await stranger.get(result["uncertain_candidates"][0]["uri"])).status_code == 404
        body = {
            "engine": "index",
            "investigation_id": desk["id"],
            "request_id": "partial-lead",
            "generation": 0,
            "question": question,
        }
        answer = await client.post("/api/index-chat", json=body)
        assert answer.status_code == 200, answer.text
        assert answer.json()["response"]["status"] == "no_result"
        assert answer.json()["response"]["events"] == []
        assert answer.json()["index_search"]["model_requests_sent"] == 0
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_owned_semantic_occurrences_and_durable_zero_model_chat(tmp_path, monkeypatch):
    app, client, video, desk, session, index = await setup_index_page(tmp_path, monkeypatch)
    try:
        with app.state.agent.ledger._connect() as c:
            calls_before = c.execute("SELECT count(*) FROM attempts").fetchone()[0]
        for question in (
            "找出穿白色球衣的人，列出所有出现时间",
            "find people wearing white jersey, list all times",
        ):
            first = await client.post(
                f"/api/videos/{video}/occurrences/search",
                json={"question": question, "generation": 1, "limit": 1},
            )
            assert first.status_code == 200, first.text
            page = first.json()
            assert page["total_occurrences"] == 3
            assert page["occurrences"][0]["start"] == 0 and page["occurrences"][0]["end"] == 2
            assert page["coverage"]["exhaustive_under_index"]
            next_page = (
                await client.post(
                    f"/api/videos/{video}/occurrences/search",
                    json={
                        "question": question,
                        "generation": 1,
                        "offset": 1,
                        "limit": 2,
                        "snapshot": page["snapshot"],
                    },
                )
            ).json()
            assert [(h["start"], h["end"]) for h in next_page["occurrences"]] == [(4, 6), (10, 12)]
            for hit in next_page["occurrences"]:
                assert (await client.get(hit["evidence"][0]["uri"])).status_code == 200
        body = {
            "engine": "index",
            "investigation_id": desk["id"],
            "request_id": "offline_index_1",
            "generation": 0,
            "question": "找出穿白色球衣的人，列出所有出现时间",
        }
        scoped = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={"question": "请在00:04至00:06找出穿白色球衣的人", "generation": 1},
        )
        assert scoped.status_code == 200, scoped.text
        assert [(hit["start"], hit["end"]) for hit in scoped.json()["occurrences"]] == [(4, 6)]
        point = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={"question": "请在00:04找出穿白色球衣的人", "generation": 1},
        )
        assert point.status_code == 200, point.text
        assert point.json()["coverage"]["time_scope"] == {"start": 4, "end": 5}
        assert [(hit["start"], hit["end"]) for hit in point.json()["occurrences"]] == [(4, 5)]
        text_only = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={"question": "请在00:04找文字“EXIT”", "generation": 1},
        )
        assert text_only.status_code == 200 and text_only.json()["ocr_eligible"] is False
        additional_attribute = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={"question": "请在00:04找红色文字“EXIT”", "generation": 1},
        )
        assert additional_attribute.status_code == 200
        assert additional_attribute.json()["ocr_eligible"] is False
        explicit_filter = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={
                "question": "请在00:04找文字“EXIT”",
                "generation": 1,
                "conditions": [{"field": "kind", "value": "person", "operator": "is"}],
            },
        )
        assert explicit_filter.status_code == 200
        assert explicit_filter.json()["ocr_eligible"] is False
        assert (await client.post("/api/index-chat/prepare", json=body)).json()["kind"] == "ready"
        sent = await client.post("/api/index-chat", json=body)
        assert sent.status_code == 200, sent.text
        result = sent.json()
        assert result["response"]["status"] == "partial" and len(result["response"]["events"]) == 3
        assert "cited frame times:" in result["response"]["answer"]
        assert (
            result["response"]["events"][0]["evidence"][0]["t_hms"] in result["response"]["answer"]
        )
        assert result["engine"] == "index" and result["index_search"]["model_requests_sent"] == 0
        assert (await client.post("/api/index-chat", json=body)).json() == result
        conflict = await client.post("/api/index-chat", json=body | {"question": "blue jersey"})
        assert conflict.status_code == 409
        follow = await client.post(
            "/api/index-chat",
            json=body
            | {
                "request_id": "offline_follow",
                "question": "同样条件",
                "parent_request_id": body["request_id"],
            },
        )
        assert follow.status_code == 200 and follow.json()["usedContext"]
        assert len(follow.json()["response"]["events"]) == 3
        restored = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()
        assert len(restored["items"]) == 2
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == calls_before == 0
        bad = await client.post(
            f"/api/videos/{video}/occurrences/search",
            json={"question": "white jersey jumping", "generation": 1},
        )
        assert bad.json()["kind"] == "clarification_required"
        assert (
            await client.post(
                f"/api/videos/{video}/occurrences/search",
                json={"question": "white jersey", "generation": 99},
            )
        ).status_code == 409
        assert (
            await client.post("/api/index-chat", json=body, headers={"x-fs-csrf": "bad"})
        ).status_code == 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as other:
            s = (await other.get("/api/session")).json()
            other.headers["x-fs-csrf"] = s["csrf"]
            assert (await other.post("/api/index-chat", json=body)).status_code == 404
    finally:
        await client.aclose()


def test_sampled_answer_labels_partial_timestamp_list_and_keeps_exact_frames():
    from test_occurrences import CRITERIA, catalog, checkpoint, observation

    from frameseek.index_service import chat_answer
    from frameseek.occurrences import search_occurrences

    index = catalog(5, complete=False)
    data = index.model_dump(mode="json")
    for frame in data["frames"]:
        frame["t"] += 0.125
    index = type(index).model_validate(data)
    index = checkpoint(index, [observation(i) for i in range(5)], index.snapshot())
    result = search_occurrences(index, CRITERIA, limit=5)
    result.update(video_id=index.video, duration=index.duration, question="find red hats")
    answer = chat_answer(result)
    assert (
        "5 matching sampled frame times; this is not a count of people or events"
        in answer["answer"]
    )
    assert "Recognition is unverified; review the cited frames" in answer["answer"]
    assert "subject in the saved frame" in answer["events"][0]["summary"]
    assert "Some cited frame times: 00:00:00.125, 00:00:01.125, 00:00:02.125" in answer["answer"]
    assert "00:00:03.125" not in answer["answer"]
    gender_result = result | {"conditions": [{"field": "gender", "value": "male"}]}
    assert "identity and gender identity are unverified" in chat_answer(gender_result)["answer"]
    assert [event["evidence"][0]["t"] for event in answer["events"]] == [
        0.125,
        1.125,
        2.125,
        3.125,
        4.125,
    ]
    assert "prevent a claim of all appearances" in answer["answer"]


def test_data_driven_arbitrary_semantic_alias_and_same_subject_binding():
    from test_occurrences import catalog, checkpoint, observation

    from frameseek.occurrences import ObservationIndex, search_occurrences
    from frameseek.semantic_search import resolve_conditions

    index = catalog(1)
    index = checkpoint(
        index,
        [observation(0, subjects=[{"id": "statue", "facts": {"材质": ["铜"], "类别": ["雕塑"]}}])],
        index.snapshot(),
    )
    data = index.model_dump()
    data["query_aliases"] = {
        "metal sculpture": [{"field": "材质", "value": "铜"}, {"field": "类别", "value": "雕塑"}],
        "铜像": [{"field": "材质", "value": "铜"}, {"field": "类别", "value": "雕塑"}],
    }
    index = ObservationIndex.model_validate(data)
    for question in ("find all metal sculpture", "找出铜像，列出所有出现时间"):
        conditions, problems = resolve_conditions(index, question)
        assert not problems and len(conditions) == 2
        assert search_occurrences(index, conditions)["total_occurrences"] == 1


@pytest.mark.anyio
async def test_event_count_ranking_replay_evidence_and_recovery_without_models(
    tmp_path, monkeypatch
):
    app, client, video, desk, session, index = await setup_index_page(
        tmp_path, monkeypatch, event_fixture=True
    )
    body = {
        "engine": "index",
        "investigation_id": desk["id"],
        "request_id": "event-count",
        "generation": 0,
        "question": "搬运多少次？",
    }
    try:
        assert (await client.post("/api/index-chat/prepare", json=body)).json()["kind"] == "ready"
        response = await client.post("/api/index-chat", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["index_search"]["aggregation"]["unique_events"] == 2
        assert result["index_search"]["aggregation"]["repeated_clips"] == 1
        assert "grouped into 2 events" in result["response"]["answer"]
        assert len(result["response"]["events"]) == 3
        for event in result["response"]["events"]:
            for frame in event["evidence"]:
                assert (await client.get(frame["uri"])).status_code == 200
        assert (await client.post("/api/index-chat", json=body)).json() == result
        ranked = (
            await client.post(
                "/api/index-chat",
                json=body | {"request_id": "event-ranking", "question": "谁搬运最多？"},
            )
        ).json()
        assert ranked["index_search"]["aggregation"]["ranking_complete"]
        assert "entity-one: 2 groups" in ranked["response"]["answer"]
        transitioned = (
            await client.post(
                "/api/index-chat",
                json=body | {"request_id": "state-transitions", "question": "开门多少次？"},
            )
        ).json()
        assert transitioned["index_search"]["aggregation"]["unique_events"] == 2
        assert transitioned["index_search"]["aggregation"]["exact_under_index"]
        restored = (await client.get(f"/api/investigations/{desk['id']}/requests")).json()
        assert len(restored["items"]) == 3
        assert (
            await client.post(
                "/api/index-chat",
                json=body | {"request_id": "unauthorized"},
                headers={"x-fs-csrf": "wrong"},
            )
        ).status_code == 403
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        await client.aclose()
