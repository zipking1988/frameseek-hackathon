"""Owned asynchronous on-device chat; no remote or paid fallback."""

import json
import threading
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from frameseek.demo import DEMO_IDS
from frameseek.local_vision import answer_source, cancel_worker, configured
from frameseek.question_context import matches_source_generation, resolve_followup, saved_question
from frameseek.schema import Answer, QueryControl
from frameseek.workspace import digest

_SLOT = threading.BoundedSemaphore(1)
_CANCELLATIONS = {}
_CANCEL_LOCK = threading.Lock()


def interrupt_local_query(owner, desk, request):
    with _CANCEL_LOCK:
        callback = _CANCELLATIONS.get((owner, desk, request))
        if callback:
            callback()


def install_local_routes(router, ws, owner_dependency, ChatInput, *, cloud=None, chat_guard=None):
    prefix = "/api/cloud-chat" if cloud else "/api/local-chat"
    label = "Cloud vision" if cloud else "Local vision"

    def available():
        return cloud.configured() if cloud else configured()

    def admission(source, owner, body, context=None):
        if cloud:
            return cloud.prepare(source, owner, resolve_followup(body.question, context))
        return {"kind": "ready", "request_id": body.request_id, "paid_requests": 0}

    def owned(body, owner):
        desk = ws.owned("investigations", body.investigation_id, owner)
        if json.loads(desk["state"]).get("epoch", 0) != body.generation:
            raise HTTPException(409, "stale_chat_generation")
        if desk["video"] in DEMO_IDS:
            raise HTTPException(409, "demo_uses_explicit_fixtures")
        source = ws.owned("videos", desk["video"], owner)
        context = None
        if body.parent_request_id:
            with ws.db() as c:
                parent = c.execute(
                    "SELECT result,input FROM queries WHERE owner=? AND investigation=? AND id=? "
                    "AND generation=? AND state='done'",
                    (owner, desk["id"], body.parent_request_id, body.generation),
                ).fetchone()
            if not parent:
                raise HTTPException(409, "invalid_context_anchor")
            saved = json.loads(parent["result"])
            if saved.get("video_id") != source["id"]:
                raise HTTPException(409, "foreign_context_video")
            if not matches_source_generation(saved, source["id"], source["generation"]):
                raise HTTPException(409, "stale_context_generation")

            def load_parent(key):
                with ws.db() as c:
                    ancestor = c.execute(
                        "SELECT result,input FROM queries WHERE owner=? AND investigation=? "
                        "AND id=? AND generation=? AND state='done'",
                        (owner, desk["id"], key, body.generation),
                    ).fetchone()
                return (
                    (json.loads(ancestor["result"]), json.loads(ancestor["input"] or "{}"))
                    if ancestor
                    else None
                )

            try:
                original_question = saved_question(
                    saved,
                    json.loads(parent["input"] or "{}"),
                    load_parent,
                    video=source["id"],
                    generation=source["generation"],
                )
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            context = {
                "question": original_question,
                "answer": saved["response"]["answer"],
                "events": saved["response"]["events"],
            }
        return desk, source, context

    @router.post(prefix + "/prepare")
    def prepare(body: ChatInput, owner=owner_dependency):
        desk, source, context = owned(body, owner)
        if chat_guard:
            chat_guard(desk)
        if not available():
            return {
                "kind": "capability_blocked",
                "message": label + " is not ready; your question was kept.",
            }
        return admission(source, owner, body, context)

    def publish(body, owner, source, answer, context=None):
        try:
            resolved_question = resolve_followup(body.question, context)
        except ValueError:
            resolved_question = body.question
        terminal = {
            "id": body.request_id,
            "question": body.question,
            "resolved_question": resolved_question,
            "response": answer,
            "mode": "real",
            "engine": "cloud" if cloud else "local",
            "video_id": source["id"],
            "source_generation": source["generation"],
            "usedContext": bool(body.parent_request_id),
        }
        with ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            latest_desk = ws.owned("investigations", body.investigation_id, owner)
            latest_source = ws.owned("videos", source["id"], owner)
            if (
                latest_desk["video"] != source["id"]
                or json.loads(latest_desk["state"]).get("epoch", 0) != body.generation
                or latest_source["generation"] != source["generation"]
                or latest_source["manifest"] != source["manifest"]
            ):
                return
            c.execute(
                "UPDATE queries SET state='done',result=?,http=200,updated=? "
                "WHERE owner=? AND id=? AND state='running'",
                (json.dumps(terminal, ensure_ascii=False), time.time(), owner, body.request_id),
            )

    def work(body, owner, desk, source, context):
        cancellation_key = (owner, desk["id"], body.request_id)
        if not cloud:
            with _CANCEL_LOCK:
                _CANCELLATIONS[cancellation_key] = cancel_worker

        def active():
            try:
                current = ws.owned("investigations", desk["id"], owner)
                latest = ws.owned("videos", source["id"], owner)
                with ws.db() as c:
                    row = c.execute(
                        "SELECT state FROM queries WHERE owner=? AND id=?", (owner, body.request_id)
                    ).fetchone()
                return (
                    row
                    and row["state"] == "running"
                    and current["video"] == source["id"]
                    and json.loads(current["state"]).get("epoch", 0) == body.generation
                    and latest["generation"] == source["generation"]
                    and latest["manifest"] == source["manifest"]
                )
            except HTTPException:
                return False

        def progress(message):
            with ws.db() as c:
                c.execute(
                    "UPDATE queries SET result=?,updated=? "
                    "WHERE owner=? AND id=? AND state='running'",
                    (
                        json.dumps({"progress": message}, ensure_ascii=False),
                        time.time(),
                        owner,
                        body.request_id,
                    ),
                )

        try:
            answer = (
                cloud.answer(source, owner, body.question, context, progress, active)
                if cloud
                else answer_source(ws.root, source, body.question, context, progress, active)
            )
            if active():
                publish(body, owner, source, answer, context)
        except Exception as error:
            if active():
                answer = Answer.model_validate(
                    {
                        "schema_version": "0.1",
                        "status": "error",
                        "timebase": "video_absolute_seconds",
                        "answer": (
                            label
                            + " did not finish; the question and completed segments were saved. "
                            + (
                                "Dispatched cloud calls remain in the ledger "
                                "and will not be resent automatically."
                                if cloud
                                else "No paid API calls were made."
                            )
                        ),
                        "events": [],
                        "unresolved": [type(error).__name__ + ": " + str(error)[:200]],
                        "followups": [],
                        "errors": [
                            {
                                "code": "local_vision_failed",
                                "stage": "query",
                                "message": label + " did not complete",
                                "cause": str(error)[:200] or type(error).__name__,
                                "retryable": False,
                                "suggested_action": "Keep the question and check local "
                                "recognition logs before an explicit retry.",
                                "docs_url": "/docs/errors",
                                "request_id": body.request_id,
                            }
                        ],
                        "trace": {
                            "visible": {
                                "channels": [],
                                "segments_reviewed": 0,
                                "evidence_segments": 0,
                            },
                            "debug": None,
                        },
                    }
                ).model_dump(mode="json")
                publish(body, owner, source, answer, context)
        finally:
            with _CANCEL_LOCK:
                _CANCELLATIONS.pop(cancellation_key, None)
            _SLOT.release()

    @router.post(prefix)
    def local_chat(body: ChatInput, owner=owner_dependency):
        desk, source, context = owned(body, owner)
        if chat_guard:
            chat_guard(desk)
        payload = digest(("cloud:" if cloud else "") + body.model_dump_json())
        slot_acquired = False
        try:
            with ws.db() as c:
                c.execute("BEGIN IMMEDIATE")
                old = c.execute(
                    "SELECT * FROM queries WHERE owner=? AND id=?", (owner, body.request_id)
                ).fetchone()
                if old:
                    if old["hash"] != payload or old["investigation"] != desk["id"]:
                        raise HTTPException(409, "request_payload_conflict")
                    if old["state"] == "done":
                        return JSONResponse(
                            json.loads(old["result"]), headers={"Cache-Control": "no-store"}
                        )
                    unknown = old["state"] == "unknown"
                    return QueryControl(
                        kind="query_control",
                        state="unknown" if unknown else "pending",
                        request_id=body.request_id,
                        retry_after_seconds=None if unknown else 3,
                        suggested_action=json.loads(old["result"] or "{}").get(
                            "progress",
                            "Local recognition is running; the answer will appear when complete.",
                        ),
                    ).model_dump(mode="json")
                if not available():
                    raise HTTPException(409, "local_vision_not_configured")
                if cloud:
                    admitted = admission(source, owner, body, context)
                    if admitted["kind"] != "ready":
                        return JSONResponse(admitted, headers={"Cache-Control": "no-store"})
                if (
                    c.execute("SELECT count(*) FROM queries WHERE owner=?", (owner,)).fetchone()[0]
                    >= 100
                    or c.execute("SELECT count(*) FROM queries").fetchone()[0] >= 1000
                ):
                    raise HTTPException(429, "query_history_limit")
                if not _SLOT.acquire(blocking=False):
                    raise HTTPException(429, "local_vision_busy")
                slot_acquired = True
                c.execute(
                    "INSERT INTO queries(id,investigation,owner,generation,hash,"
                    "state,result,http,updated,input) "
                    "VALUES(?,?,?,?,?,'running',?,200,?,?)",
                    (
                        body.request_id,
                        desk["id"],
                        owner,
                        body.generation,
                        payload,
                        json.dumps({"progress": label + ": preparing recognition…"}),
                        time.time(),
                        body.model_dump_json(),
                    ),
                )
        except BaseException:
            if slot_acquired:
                _SLOT.release()
            raise
        try:
            threading.Thread(
                target=work, args=(body, owner, desk, source, context), daemon=True
            ).start()
        except Exception:
            # No worker started, so no inference was dispatched. Keep the request
            # identity terminal without replacing a concurrent cancel or reset.
            _SLOT.release()
            answer = Answer.model_validate(
                {
                    "schema_version": "0.1",
                    "status": "error",
                    "timebase": "video_absolute_seconds",
                    "answer": label + " could not start; your question was kept.",
                    "events": [],
                    "unresolved": [],
                    "followups": [],
                    "errors": [
                        {
                            "code": "worker_start_failed",
                            "stage": "query",
                            "message": "Background worker could not start",
                            "cause": "worker_start_failed",
                            "retryable": False,
                            "suggested_action": "Check local resources, then submit a new "
                            "question explicitly.",
                            "docs_url": "/docs/errors",
                            "request_id": body.request_id,
                        }
                    ],
                    "trace": {
                        "visible": {"channels": [], "segments_reviewed": 0, "evidence_segments": 0},
                        "debug": None,
                    },
                }
            ).model_dump(mode="json")
            terminal = {
                "id": body.request_id,
                "question": body.question,
                "response": answer,
                "mode": "real",
                "engine": "cloud" if cloud else "local",
                "video_id": source["id"],
                "source_generation": source["generation"],
                "usedContext": bool(body.parent_request_id),
            }
            with ws.db() as c:
                c.execute(
                    "UPDATE queries SET state='done',result=?,http=200,updated=? "
                    "WHERE owner=? AND id=? AND investigation=? AND generation=? "
                    "AND state='running'",
                    (
                        json.dumps(terminal, ensure_ascii=False),
                        time.time(),
                        owner,
                        body.request_id,
                        desk["id"],
                        body.generation,
                    ),
                )
            raise HTTPException(503, "worker_start_failed") from None
        return QueryControl(
            kind="query_control",
            state="pending",
            request_id=body.request_id,
            retry_after_seconds=3,
            suggested_action=label + ": preparing recognition…",
        ).model_dump(mode="json")
