"""Supplement retention must free expired capacity without losing live request identity."""

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from frameseek.attempts import AttemptLedger
from frameseek.knowledge_jobs import FixtureKnowledgeJobs
from frameseek.routes import ChatInput
from frameseek.workspace import Workspace


def owned_context(ws):
    session, _ = ws.session()
    owner = session["id"]
    video = "vid_" + owner[:16]
    with ws.db() as connection:
        connection.execute(
            "INSERT INTO videos(id,owner,name,bytes,expires) VALUES(?,?,'synthetic',1,?)",
            (video, owner, session["expires"]),
        )
    desk = ws.create(owner, video)
    return owner, ws.owned("videos", video, owner), desk


def insert_job(ws, owner, source, desk, request_id, state="done"):
    with ws.db() as connection:
        connection.execute(
            "INSERT INTO knowledge_supplement_jobs(owner,request_id,investigation,video,"
            "generation,desk_epoch,payload_hash,state,base_version,updated,input) "
            "VALUES(?,?,?,?,?,0,'synthetic-hash',?,'synthetic-version',?,?)",
            (
                owner,
                request_id,
                desk["id"],
                source["id"],
                source["generation"],
                state,
                time.time(),
                json.dumps({"question": "synthetic private question"}),
            ),
        )


def jobs_without_dispatch(ws, monkeypatch):
    import frameseek.knowledge_jobs as module

    dispatched = []
    monkeypatch.setattr(
        module.threading,
        "Thread",
        lambda *, target, args, daemon: SimpleNamespace(start=lambda: dispatched.append(args)),
    )
    jobs = FixtureKnowledgeJobs(ws, None, None)
    jobs.processor = lambda: object()
    return jobs, dispatched


@pytest.mark.parametrize(
    "invalidation", ["delete_desk", "delete_video", "expire_desk", "expire_video", "expire_owner"]
)
def test_invalid_context_removes_question_but_preserves_live_unknown_and_ledger(
    tmp_path, invalidation
):
    from test_budget import install, reserve

    ws = Workspace(tmp_path / "workspace")
    owner, source, desk = owned_context(ws)
    live_owner, live_source, live_desk = owned_context(ws)
    for state in ("done", "failed", "unknown", "running", "cancelled", "waiting_budget"):
        insert_job(ws, owner, source, desk, state, state)
    insert_job(ws, live_owner, live_source, live_desk, "live-unknown", "unknown")
    insert_job(ws, live_owner, live_source, live_desk, "live-done", "done")
    ledger = AttemptLedger(tmp_path / "ledger.sqlite3")
    for scope in ("*", owner):
        install(ledger, scope)
    reserve(ledger, owner=owner)
    ledger.transition("a1", "dispatched")
    ledger.transition("a1", "unknown")
    with sqlite3.connect(ledger.path) as connection:
        before = connection.iterdump()
        ledger_before = list(before)

    if invalidation.startswith("delete_"):
        ws.delete(
            "investigations" if invalidation == "delete_desk" else "videos",
            desk["id"] if invalidation == "delete_desk" else source["id"],
            owner,
        )
    else:
        table, key = {
            "expire_desk": ("investigations", desk["id"]),
            "expire_video": ("videos", source["id"]),
            "expire_owner": ("sessions", owner),
        }[invalidation]
        with ws.db() as connection:
            connection.execute(f"UPDATE {table} SET expires=0 WHERE id=?", (key,))
        ws.cleanup()
    with ws.db() as connection:
        remaining = connection.execute(
            "SELECT owner,state,input FROM knowledge_supplement_jobs"
        ).fetchall()
    assert len(remaining) == 2
    assert {row["owner"] for row in remaining} == {live_owner}
    assert {row["state"] for row in remaining} == {"done", "unknown"}
    assert all("synthetic private question" in row["input"] for row in remaining)
    with sqlite3.connect(ledger.path) as connection:
        assert list(connection.iterdump()) == ledger_before
    assert ledger.read_attempt("a1", owner)["state"] == "unknown"


def test_admission_recovers_capacity_from_expired_jobs_before_periodic_cleanup(
    tmp_path, monkeypatch
):
    ws = Workspace(tmp_path)
    old_owner, old_source, old_desk = owned_context(ws)
    owner, source, desk = owned_context(ws)
    for i in range(1000):
        insert_job(ws, old_owner, old_source, old_desk, str(i))
    with ws.db() as connection:
        connection.execute("UPDATE sessions SET expires=0 WHERE id=?", (old_owner,))
    jobs, dispatched = jobs_without_dispatch(ws, monkeypatch)
    body = ChatInput(
        investigation_id=desk["id"], request_id="new", generation=0, question="synthetic"
    )
    result = jobs.start_supplement(
        source, desk, owner, body, body.question, {"knowledge_version": "a" * 64}
    )
    assert result["state"] == "running" and len(dispatched) == 1
    with ws.db() as connection:
        assert (
            connection.execute("SELECT count(*) FROM knowledge_supplement_jobs").fetchone()[0] == 1
        )


def test_live_history_bound_and_idempotent_unknown_are_preserved(tmp_path, monkeypatch):
    from frameseek.workspace import digest

    ws = Workspace(tmp_path)
    owner, source, desk = owned_context(ws)
    body = ChatInput(
        investigation_id=desk["id"], request_id="0", generation=0, question="synthetic"
    )
    for i in range(1000):
        insert_job(ws, owner, source, desk, str(i), "unknown" if i == 0 else "done")
    with ws.db() as connection:
        connection.execute(
            "UPDATE knowledge_supplement_jobs SET payload_hash=? WHERE request_id='0'",
            (digest(body.model_dump_json()),),
        )
    jobs, dispatched = jobs_without_dispatch(ws, monkeypatch)
    assert jobs.start_supplement(source, desk, owner, body, body.question, {})["state"] == "unknown"
    with pytest.raises(HTTPException) as blocked:
        new_body = body.model_copy(update={"request_id": "new"})
        jobs.start_supplement(source, desk, owner, new_body, new_body.question, {})
    assert blocked.value.status_code == 429 and not dispatched


@pytest.mark.parametrize("invalidate", ["desk", "video", "owner"])
def test_admission_does_not_reinsert_a_request_after_context_is_invalidated(
    tmp_path, monkeypatch, invalidate
):
    ws = Workspace(tmp_path)
    owner, source, desk = owned_context(ws)
    with ws.db() as connection:
        table, key = {
            "desk": ("investigations", desk["id"]),
            "video": ("videos", source["id"]),
            "owner": ("sessions", owner),
        }[invalidate]
        connection.execute(f"UPDATE {table} SET expires=0 WHERE id=?", (key,))
    jobs, dispatched = jobs_without_dispatch(ws, monkeypatch)
    body = ChatInput(
        investigation_id=desk["id"], request_id="stale", generation=0, question="synthetic"
    )
    with pytest.raises(HTTPException):
        jobs.start_supplement(
            source, desk, owner, body, body.question, {"knowledge_version": "a" * 64}
        )
    assert not dispatched
    with ws.db() as connection:
        assert (
            connection.execute("SELECT count(*) FROM knowledge_supplement_jobs").fetchone()[0] == 0
        )


@pytest.mark.parametrize("unknown_result", [False, True])
def test_deleted_supplement_cannot_publish_or_recreate_private_input(
    tmp_path, monkeypatch, unknown_result
):
    import frameseek.knowledge_jobs
    import frameseek.knowledge_supplement
    import frameseek.video_knowledge

    ws = Workspace(tmp_path)
    owner, source, desk = owned_context(ws)
    insert_job(ws, owner, source, desk, "in-flight", "running")
    with ws.db() as connection:
        connection.execute(
            "INSERT INTO knowledge_jobs(video,generation,state,version,updated,cache_key) "
            "VALUES(?,1,'ready',?,?,'synthetic-cache')",
            (source["id"], "a" * 64, time.time()),
        )

    def deleted_during_processing(*args, **kwargs):
        ws.delete("investigations", desk["id"], owner)
        if unknown_result:
            raise ValueError("knowledge_dispatch_unknown")
        return "b" * 64

    monkeypatch.setattr(frameseek.knowledge_jobs, "source_digest", lambda _: "source")
    monkeypatch.setattr(frameseek.video_knowledge, "load_published_state", lambda *a: object())
    monkeypatch.setattr(frameseek.knowledge_jobs, "KnowledgeStore", lambda *a: object())
    monkeypatch.setattr(
        frameseek.knowledge_supplement, "compile_local_supplement", deleted_during_processing
    )
    body = ChatInput(
        investigation_id=desk["id"], request_id="in-flight", generation=0, question="synthetic"
    )
    jobs = FixtureKnowledgeJobs(ws, None, None)
    jobs._supplement_work(
        source, desk, owner, body, body.question, {"knowledge_version": "a" * 64}, object()
    )
    with ws.db() as connection:
        assert (
            connection.execute("SELECT count(*) FROM knowledge_supplement_jobs").fetchone()[0] == 0
        )
        assert connection.execute("SELECT version FROM knowledge_jobs").fetchone()[0] == "a" * 64
