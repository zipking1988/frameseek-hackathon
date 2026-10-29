"""Session-owned workspace, upload/jobs, private media and idempotent chat."""

import asyncio
import fcntl
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from frameseek.agent import Agent, AgentError, explicit_time_window
from frameseek.attempts import BudgetDenied
from frameseek.demo import catalog
from frameseek.local_vision import configured as local_configured
from frameseek.media import MAX_UPLOAD, MediaError, frames, probe, scene_windows
from frameseek.readiness import budget_ready, readiness_report
from frameseek.schema import Answer
from frameseek.workspace import Workspace, digest, ident

Key = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]
_PROCESS_LOCKS = {}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NewInvestigation(Input):
    video_id: Key = "demo_parcel"
    title: str = Field(default="未命名调查", max_length=80)


class SaveInvestigation(Input):
    revision: int = Field(ge=0)
    state: dict


class RenameVideo(Input):
    name: str = Field(min_length=1, max_length=180)


class ChatInput(Input):
    investigation_id: Key
    request_id: Key
    generation: int = Field(ge=0)
    question: str = Field(min_length=1, max_length=4000)
    parent_request_id: Key | None = None


class IndexChatInput(ChatInput):
    engine: Literal["index"] = "index"


class OccurrenceSearchInput(Input):
    question: str = Field(min_length=1, max_length=4000)
    generation: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=216000)
    candidate_offset: int = Field(default=0, ge=0, le=216000)
    candidate_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    limit: int = Field(default=40, ge=1, le=100)
    snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    conditions: list[dict] | None = Field(default=None, max_length=12)
    answer_request_id: Key | None = None


class OcrCandidateInput(Input):
    question: str = Field(min_length=1, max_length=4000)
    generation: int = Field(ge=1)


class IndexSearchInput(Input):
    offset: int = Field(default=0, ge=0, le=216000)
    limit: int = Field(default=40, ge=1, le=100)
    snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    all_saved: bool = False
    question: str = Field(min_length=1, max_length=4000)
    generation: int = Field(ge=1)


def unknown_final(request_id):
    return {
        "id": request_id,
        "question": "先前的查询结果不确定",
        "mode": "real",
        "response": {
            "schema_version": "0.1",
            "status": "error",
            "timebase": "video_absolute_seconds",
            "answer": "先前调用结果不确定，已结束恢复。",
            "events": [],
            "unresolved": [],
            "followups": [],
            "trace": {
                "visible": {"channels": [], "segments_reviewed": 0, "evidence_segments": 0},
                "debug": None,
            },
            "errors": [
                {
                    "code": "model_unknown",
                    "stage": "query",
                    "message": "调用结果不确定",
                    "cause": "unknown",
                    "retryable": False,
                    "suggested_action": "显式发起新的尝试。",
                    "docs_url": "/docs/errors",
                    "request_id": request_id,
                }
            ],
        },
    }


def register(app, settings, root, runtime=None, transport=None):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if str(root) in _PROCESS_LOCKS:
        raise RuntimeError("One API/worker instance is allowed per private workspace")
    lock = os.open(root / "serve.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        raise RuntimeError("Another API/worker process owns this workspace") from None
    _PROCESS_LOCKS[str(root)] = lock
    app.state.process_lock = lock
    ws = Workspace(root, settings.ttl_hours)
    agent = Agent(ws, settings, runtime, transport)
    from frameseek.cloud_vision import CloudVision

    cloud_vision = CloudVision(agent)
    app.state.workspace, app.state.agent = ws, agent
    router = APIRouter()
    upload_gate = asyncio.Semaphore(2)
    index_gate = threading.BoundedSemaphore(2)
    worker_lock = threading.Lock()
    from frameseek.knowledge_jobs import FixtureKnowledgeJobs

    knowledge_jobs = FixtureKnowledgeJobs(ws, app, settings)

    def knowledge_enabled():
        return bool(
            settings.mode == "fixture"
            and (
                getattr(app.state, "knowledge_fixture_refactor", False)
                or os.environ.get("FS_KNOWLEDGE_LOCAL") == "1"
            )
            or settings.mode == "real"
            and os.environ.get("FS_KNOWLEDGE_TOKENFACTORY") == "1"
        )

    def require_unified_chat(desk):
        if knowledge_enabled() and desk["video"] != "demo_parcel":
            raise HTTPException(409, "use_unified_knowledge_chat")

    def knowledge_status(video, owner, *, source=None, connection=None):
        source = source or ws.owned("videos", video, owner)
        if connection is None:
            with ws.db() as owned_connection:
                row = owned_connection.execute(
                    "SELECT * FROM knowledge_jobs WHERE video=? AND generation=?",
                    (video, source["generation"]),
                ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE video=? AND generation=?",
                (video, source["generation"]),
            ).fetchone()
        return {
            "state": row["state"] if row else "not_started",
            "version": row["version"] if row else None,
            "completed_segments": row["completed"] if row else 0,
            "total_segments": row["total"] if row else 0,
            "error": row["error"] if row else None,
            "coverage": "sampled_frames_only" if row and row["version"] else "not_indexed",
            "capabilities": {"speech_transcription": "not_enabled"},
            "required_for_chat": knowledge_enabled(),
            "ocr_candidates_available": bool(
                knowledge_enabled()
                and os.environ.get("FS_OCR_CANDIDATES") == "1"
                and shutil.which("tesseract")
            ),
            "processor_available": (
                bool(
                    getattr(
                        getattr(app.state, "knowledge_fixture_processor", None),
                        "fixture_only",
                        False,
                    )
                    or (os.environ.get("FS_KNOWLEDGE_LOCAL") == "1" and local_configured())
                )
                if knowledge_enabled() and settings.mode == "fixture"
                else None
            ),
            "processor": (
                "synthetic_fixture"
                if knowledge_enabled()
                and getattr(
                    getattr(app.state, "knowledge_fixture_processor", None), "fixture_only", False
                )
                else "local"
                if knowledge_enabled() and settings.mode == "fixture"
                else "tokenfactory"
                if knowledge_enabled() and settings.mode == "real"
                else None
            ),
        }

    workers = set()

    def owned(request: Request):
        session = ws.authenticate(request.cookies.get("fs_session"))
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            expected_origin = os.environ.get("FS_PUBLIC_ORIGIN", str(request.base_url)).rstrip("/")
            if origin and origin != expected_origin:
                raise HTTPException(403, "origin_rejected")
            if not request.headers.get("x-fs-csrf") or not __import__("secrets").compare_digest(
                request.headers["x-fs-csrf"], session["csrf"]
            ):
                raise HTTPException(403, "csrf_rejected")
        return session["id"]

    owner_dependency = Depends(owned)

    @router.post("/api/videos/{key}/search-index")
    def search_index(key: str, body: IndexSearchInput, response: Response, owner=owner_dependency):
        from frameseek.visual_search import compile_index, retrieve, retrieve_page

        source = ws.owned("videos", key, owner)
        if source["generation"] != body.generation:
            raise HTTPException(409, "stale_video_generation")
        if not source["manifest"]:
            raise HTTPException(409, "video_not_ready")
        try:
            metadata = json.loads(source["metadata"])
            index = compile_index(
                key, body.generation, metadata["duration"], json.loads(source["manifest"])
            )
            result = (
                retrieve_page(index, body.question, body.offset, body.limit, body.snapshot)
                if body.all_saved
                else retrieve(index, body.question)
            )
            directory = ws.root / key / "frames"
            for frame in result["frames"]:
                path = directory / frame["file"]
                if not path.is_file() or path.resolve().parent != directory.resolve():
                    raise ValueError("missing indexed frame")
        except (KeyError, TypeError, ValueError, OSError):
            raise HTTPException(409, "visual_index_invalid") from None
        response.headers["Cache-Control"] = "no-store"
        return {
            "kind": "index_candidates",
            "video_id": key,
            "generation": body.generation,
            "question": body.question,
            "verified_matches": False,
            "model_requests_sent": 0,
            **result,
        }

    def current_knowledge(source, version=None):
        from frameseek.index_service import source_digest
        from frameseek.video_knowledge import load_published_knowledge

        with ws.db() as c:
            job = c.execute(
                "SELECT state,version,cache_key FROM knowledge_jobs WHERE video=? AND generation=?",
                (source["id"], source["generation"]),
            ).fetchone()
        if not job or job["state"] != "ready" or not job["version"]:
            raise HTTPException(409, "knowledge_index_not_ready")
        directory = root / source["id"]
        version = version or job["version"]
        index = load_published_knowledge(
            directory,
            source["id"],
            source["generation"],
            job["cache_key"],
            version,
            source_digest(directory),
        )
        return index, version

    def indexed_search(
        source,
        question,
        offset=0,
        limit=40,
        snapshot=None,
        conditions=None,
        version=None,
        candidate_offset=0,
        candidate_time=None,
        include_candidates=False,
    ):
        from frameseek.index_service import search_source

        if not index_gate.acquire(blocking=False):
            raise HTTPException(429, "index_search_capacity")
        try:
            knowledge = None
            if knowledge_enabled():
                knowledge, version = current_knowledge(source, version)
            elif version:
                raise ValueError("knowledge_index_unavailable")
            result = search_source(
                root,
                source,
                question,
                offset,
                limit,
                snapshot,
                conditions,
                knowledge=knowledge,
                candidate_offset=candidate_offset,
                candidate_time=candidate_time,
                include_candidates=include_candidates,
            )
            from frameseek.ocr_candidates import eligible_index_scope

            result["ocr_eligible"] = not conditions and eligible_index_scope(knowledge, question)
            if version:
                result["knowledge_version"] = version
        except (ValueError, KeyError, TypeError, OSError):
            raise HTTPException(409, "observation_index_unavailable_or_stale") from None
        finally:
            index_gate.release()
        current = ws.owned("videos", source["id"], source["owner"])
        if (
            current["generation"] != source["generation"]
            or current["manifest"] != source["manifest"]
        ):
            raise HTTPException(409, "stale_video_generation")
        return result

    @router.post("/api/videos/{key}/occurrences/search")
    def occurrence_search(
        key: str, body: OccurrenceSearchInput, response: Response, owner=owner_dependency
    ):
        source = ws.owned("videos", key, owner)
        if source["generation"] != body.generation:
            raise HTTPException(409, "stale_video_generation")
        version = None
        if body.answer_request_id:
            with ws.db() as c:
                row = c.execute(
                    "SELECT investigation,result FROM queries "
                    "WHERE owner=? AND id=? AND state='done'",
                    (owner, body.answer_request_id),
                ).fetchone()
            if not row:
                raise HTTPException(409, "invalid_index_answer_anchor")
            desk = ws.owned("investigations", row["investigation"], owner)
            saved = json.loads(row["result"])
            if (
                desk["video"] != key
                or saved.get("engine") != "index"
                or saved.get("source_generation") != source["generation"]
            ):
                raise HTTPException(409, "invalid_index_answer_anchor")
            version = (saved.get("index_search") or {}).get("knowledge_version")
            if not version:
                raise HTTPException(409, "index_answer_version_unavailable")
        response.headers["Cache-Control"] = "no-store"
        return indexed_search(
            source,
            body.question,
            body.offset,
            body.limit,
            body.snapshot,
            body.conditions,
            version,
            candidate_offset=body.candidate_offset,
            candidate_time=body.candidate_time,
            include_candidates=True,
        )

    @router.post("/api/videos/{key}/ocr-candidates")
    def ocr_candidates(
        key: str, body: OcrCandidateInput, response: Response, owner=owner_dependency
    ):
        if os.environ.get("FS_OCR_CANDIDATES") != "1" or not knowledge_enabled():
            raise HTTPException(409, "ocr_candidates_not_enabled")
        source = ws.owned("videos", key, owner)
        if source["generation"] != body.generation:
            raise HTTPException(409, "stale_video_generation")
        if not index_gate.acquire(blocking=False):
            raise HTTPException(429, "index_search_capacity")
        try:
            from frameseek.ocr_candidates import find_candidates

            index, version = current_knowledge(source)
            result = find_candidates(root / key, index, body.question)
            # Reject results if the source or published version changed during OCR.
            latest = ws.owned("videos", key, owner)
            if (
                latest["generation"] != source["generation"]
                or current_knowledge(latest)[1] != version
            ):
                raise HTTPException(409, "knowledge_version_changed")
            response.headers["Cache-Control"] = "no-store"
            return {**result, "knowledge_version": version}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        finally:
            index_gate.release()

    def checked_supplement_request(connection, body, owner, source):
        row = connection.execute(
            "SELECT * FROM knowledge_supplement_jobs WHERE owner=? AND request_id=?",
            (owner, body.request_id),
        ).fetchone()
        if row:
            chat_body = ChatInput.model_validate(body.model_dump(exclude={"engine"}))
            if (
                row["payload_hash"] != digest(chat_body.model_dump_json())
                or row["investigation"] != body.investigation_id
                or row["video"] != source["id"]
                or row["generation"] != source["generation"]
                or row["desk_epoch"] != body.generation
            ):
                raise HTTPException(409, "request_payload_conflict")
        return row

    def index_chat_input(body, owner, *, allow_model_context=False):
        desk = ws.owned("investigations", body.investigation_id, owner)
        if json.loads(desk["state"]).get("epoch", 0) != body.generation:
            raise HTTPException(409, "stale_chat_generation")
        if desk["video"] == "demo_parcel":
            raise HTTPException(409, "demo_uses_explicit_fixtures")
        source = ws.owned("videos", desk["video"], owner)
        with ws.db() as c:
            checked_supplement_request(c, body, owner, source)
        question = body.question
        if body.parent_request_id:
            with ws.db() as c:
                parent = c.execute(
                    "SELECT result FROM queries WHERE owner=? AND id=? AND investigation=? "
                    "AND generation=? AND state='done'",
                    (owner, body.parent_request_id, desk["id"], body.generation),
                ).fetchone()
            if not parent:
                raise HTTPException(409, "invalid_context_anchor")
            prior_result = json.loads(parent["result"])
            if (
                prior_result.get("engine") == "index"
                and prior_result.get("source_generation") != source["generation"]
            ):
                raise HTTPException(409, "stale_index_context")
            if re.fullmatch(
                r"\s*(?:继续|还有吗|再找一次|同样条件|same criteria|again)[？?。\s]*",
                question,
                re.I,
            ):
                if prior_result.get("engine") != "index":
                    if not allow_model_context:
                        raise HTTPException(409, "index_context_required")
                else:
                    question = (prior_result.get("index_search") or {}).get(
                        "resolved_question", prior_result["question"]
                    )
            if prior_result.get("engine") == "index":
                from frameseek.question_context import resolve_followup

                previous = (prior_result.get("index_search") or {}).get(
                    "resolved_question", prior_result["question"]
                )
                try:
                    question = resolve_followup(question, {"question": previous})
                except ValueError as error:
                    raise HTTPException(409, str(error)) from None
        return desk, source, question

    def completed_question_supplement(body, owner, desk, source, version):
        if not version:
            return None
        with ws.db() as connection:
            candidates = connection.execute(
                "SELECT request_id,state,version,base_version FROM knowledge_supplement_jobs "
                "WHERE owner=? AND investigation=? AND video=? AND generation=? "
                "AND desk_epoch=? AND state='done' "
                "AND json_extract(input, '$.question')=? "
                "AND json_extract(input, '$.parent_request_id') IS ? ORDER BY updated DESC",
                (
                    owner,
                    desk["id"],
                    source["id"],
                    source["generation"],
                    body.generation,
                    body.question,
                    body.parent_request_id,
                ),
            ).fetchall()
            job = connection.execute(
                "SELECT cache_key FROM knowledge_jobs WHERE video=? AND generation=?",
                (source["id"], source["generation"]),
            ).fetchone()
        if not candidates:
            return None
        for candidate in candidates:
            if candidate["version"] == version:
                return candidate
        if not job:
            raise HTTPException(409, "observation_index_unavailable_or_stale")
        from frameseek.index_service import source_digest
        from frameseek.video_knowledge import load_published_state

        try:
            directory = root / source["id"]
            source_hash = source_digest(directory)

            def load(revision):
                return load_published_state(
                    directory,
                    source["id"],
                    source["generation"],
                    job["cache_key"],
                    revision,
                    source_hash,
                )

            current = load(version)
            current_by_base = {item.source_version: item for item in current.supplements}
            if len(current_by_base) != len(current.supplements):
                raise ValueError("supplement_provenance_ambiguous")
            for candidate in candidates:
                current_item = current_by_base.get(candidate["base_version"])
                if current_item is None:
                    continue
                older = load(candidate["version"])
                original = [
                    item
                    for item in older.supplements
                    if item.source_version == candidate["base_version"]
                ]
                if len(original) == 1 and original[0] == current_item:
                    return candidate
        except (ValueError, KeyError, TypeError, OSError):
            # An unverifiable receipt cannot authorize an automatic new dispatch.
            raise HTTPException(409, "observation_index_unavailable_or_stale") from None
        return None

    @router.post("/api/index-chat/prepare")
    def prepare_index_chat(body: IndexChatInput, owner=owner_dependency):
        desk, source, question = index_chat_input(body, owner)
        with ws.db() as c:
            old = c.execute(
                "SELECT hash FROM queries WHERE owner=? AND id=?", (owner, body.request_id)
            ).fetchone()
        if old:
            if old["hash"] != digest(body.model_dump_json()):
                raise HTTPException(409, "request_payload_conflict")
            return {"kind": "ready", "request_id": body.request_id}
        result = indexed_search(source, question, limit=1)
        if result["kind"] == "clarification_required":
            if completed_question_supplement(
                body, owner, desk, source, result.get("knowledge_version")
            ):
                return {"kind": "ready", "request_id": body.request_id, "model_requests_sent": 0}
            return result
        return {"kind": "ready", "request_id": body.request_id, "model_requests_sent": 0}

    @router.post("/api/index-chat")
    def index_chat(body: IndexChatInput, owner=owner_dependency):
        from frameseek.index_service import chat_answer

        desk, source, question = index_chat_input(body, owner)
        payload_hash = digest(body.model_dump_json())
        # Computation has no external side effects. Publication is a single CAS
        # transaction; no running provider attempt or model slot is created.
        with ws.db() as c:
            old = c.execute(
                "SELECT * FROM queries WHERE owner=? AND id=?", (owner, body.request_id)
            ).fetchone()
        if old:
            if old["hash"] != payload_hash:
                raise HTTPException(409, "request_payload_conflict")
            if old["state"] != "done":
                raise HTTPException(409, "index_request_not_terminal")
            return JSONResponse(json.loads(old["result"]), headers={"Cache-Control": "no-store"})
        result = indexed_search(source, question, limit=20)
        completed_supplement = None
        checked_frame_refs = []
        if result["kind"] == "clarification_required":
            completed_supplement = completed_question_supplement(
                body, owner, desk, source, result.get("knowledge_version")
            )
            if not completed_supplement:
                return JSONResponse(result, status_code=409)
            from frameseek.schema import Answer
            from frameseek.video_knowledge import load_published_state

            with ws.db() as c:
                job = c.execute(
                    "SELECT cache_key FROM knowledge_jobs WHERE video=? AND generation=?",
                    (source["id"], source["generation"]),
                ).fetchone()
            if not job:
                raise HTTPException(409, "observation_index_unavailable_or_stale")
            try:
                from frameseek.index_service import source_digest

                state = load_published_state(
                    root / source["id"],
                    source["id"],
                    source["generation"],
                    job["cache_key"],
                    result["knowledge_version"],
                    source_digest(root / source["id"]),
                )
                checked = [
                    item
                    for item in state.supplements
                    if item.source_version == completed_supplement["base_version"]
                ]
                if len(checked) != 1:
                    raise ValueError("supplement_provenance_ambiguous")
                checked_frames = len(checked[0].observations)
                frame_map = {
                    frame.frame_id: frame for segment in state.segments for frame in segment.frames
                }
                frame_dir = root / source["id"] / "frames"
                if frame_dir.is_symlink() or not frame_dir.is_dir():
                    raise ValueError("indexed_evidence_missing")
                for observation in checked[0].observations:
                    frame = frame_map[observation.frame_id]
                    path = frame_dir / frame.file
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.resolve().parent != frame_dir.resolve()
                    ):
                        raise ValueError("indexed_evidence_missing")
                    checked_frame_refs.append(
                        {"frame_id": frame.frame_id, "t": frame.t, "uri": frame.uri}
                    )
                checked_frame_refs.sort(key=lambda frame: frame["t"])
            except (ValueError, KeyError, TypeError, OSError):
                raise HTTPException(409, "observation_index_unavailable_or_stale") from None

            answer = Answer.model_validate(
                {
                    "schema_version": "0.1",
                    "status": "no_result",
                    "timebase": "video_absolute_seconds",
                    "answer": "相关画面已补查，但系统仍无法确认满足全部条件的画面。"
                    "识别可能遗漏，不能断定视频中不存在。",
                    "events": [],
                    "unresolved": [],
                    "followups": [],
                    "errors": [],
                    "trace": {
                        "visible": {
                            "channels": [],
                            "segments_reviewed": checked_frames,
                            "evidence_segments": 0,
                        },
                        "debug": None,
                    },
                }
            ).model_dump(mode="json")
            result.update(
                kind="supplement_unconfirmed",
                total_candidates=0,
                coverage={
                    "reviewed_frames": checked_frames,
                    "recognition_accuracy_verified": False,
                    "exhaustive_under_index": False,
                },
                provenance="bounded_supplement_unconfirmed",
            )
        else:
            answer = chat_answer(result)
        from frameseek.question_context import CONTINUATION, PRONOUN_TIME

        if (
            (PRONOUN_TIME.fullmatch(body.question) or CONTINUATION.fullmatch(body.question))
            and question != body.question
            and not any(
                condition.get("field") == "text" for condition in result.get("conditions", [])
            )
        ):
            answer["answer"] += " 本次按上一问题的描述匹配，未确认跨镜头为同一对象。"
        with ws.db() as c:
            supplement = checked_supplement_request(c, body, owner, source)
        if supplement and supplement["state"] == "running":
            raise HTTPException(409, "supplement_still_running")
        supplemented = bool(
            supplement
            and supplement["state"] == "done"
            and supplement["version"] == result.get("knowledge_version")
        )
        cached_supplement = bool(
            completed_supplement and completed_supplement["request_id"] != body.request_id
        )
        supplement_incomplete = bool(
            supplement and supplement["state"] in {"failed", "unknown", "waiting_budget"}
        )
        synthetic = bool(
            knowledge_enabled()
            and getattr(
                getattr(app.state, "knowledge_fixture_processor", None), "fixture_only", False
            )
        )
        if supplemented:
            cloud_supplement = settings.mode == "real"
            description = (
                "合成测试数据" if synthetic else "云端视觉" if cloud_supplement else "本机视觉"
            )
            answer["answer"] = answer["answer"].replace(
                "本次模型调用0次。",
                f"结果包含{description}补查的观察。"
                if cloud_supplement
                else f"结果包含{description}补查的观察，未调用云端模型。",
            )
            for event in answer["events"]:
                for evidence in event["evidence"]:
                    evidence["caption"] = "引用更新后的结构化观察；请回看原视频画面。"
            if completed_supplement:
                answer["answer"] += " 本次处理包含此前完成的局部补查。"
        elif cached_supplement:
            answer["answer"] += " 已复用之前的补查，本次未调用模型。"
        elif supplement_incomplete:
            answer["answer"] = answer["answer"].replace(
                "本次模型调用0次。", "补查未完成或结果不明；以下结果来自已有索引。"
            )
        terminal = {
            "id": body.request_id,
            "question": body.question,
            "response": answer,
            "mode": "real",
            "synthetic_fixture": synthetic,
            "engine": "index",
            "video_id": source["id"],
            "usedContext": question != body.question,
            "source_generation": source["generation"],
            "index_search": {
                "kind": result["kind"],
                "resolved_question": question,
                "total": result.get("total_occurrences", result.get("total_candidates", 0)),
                "coverage": {k: v for k, v in result["coverage"].items() if not k.endswith("_ids")},
                "model_requests_sent": None
                if (supplemented or supplement_incomplete) and not synthetic
                else 0,
                "cloud_requests_sent": None
                if (supplemented or supplement_incomplete) and settings.mode == "real"
                else 0,
                "aggregation": result.get("aggregation"),
                "knowledge_version": result.get("knowledge_version"),
                "provenance": result.get("provenance"),
                "supplemented": supplemented,
                "supplement_reused": cached_supplement,
                "checked_frames": checked_frame_refs,
                "ocr_eligible": result.get("ocr_eligible", False),
                "supplement_incomplete": supplement_incomplete,
            },
        }
        with ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            pending_supplement = checked_supplement_request(c, body, owner, source)
            if pending_supplement and pending_supplement["state"] == "running":
                raise HTTPException(409, "supplement_still_running")
            old = c.execute(
                "SELECT * FROM queries WHERE owner=? AND id=?", (owner, body.request_id)
            ).fetchone()
            if old:
                if old["hash"] != payload_hash:
                    raise HTTPException(409, "request_payload_conflict")
                if old["state"] != "done":
                    raise HTTPException(409, "index_request_not_terminal")
                return JSONResponse(
                    json.loads(old["result"]), headers={"Cache-Control": "no-store"}
                )
            active = ws.owned("investigations", desk["id"], owner)
            latest = ws.owned("videos", source["id"], owner)
            if (
                json.loads(active["state"]).get("epoch", 0) != body.generation
                or latest["generation"] != source["generation"]
                or latest["manifest"] != source["manifest"]
            ):
                raise HTTPException(409, "stale_chat_generation")
            if (
                c.execute("SELECT count(*) FROM queries WHERE owner=?", (owner,)).fetchone()[0]
                >= 100
                or c.execute("SELECT count(*) FROM queries").fetchone()[0] >= 1000
            ):
                raise HTTPException(429, "query_history_limit")
            c.execute(
                "INSERT INTO queries(id,investigation,owner,generation,hash,state,"
                "result,http,updated,input) "
                "VALUES(?,?,?,?,?,'done',?,200,?,?)",
                (
                    body.request_id,
                    desk["id"],
                    owner,
                    body.generation,
                    payload_hash,
                    json.dumps(terminal, ensure_ascii=False),
                    time.time(),
                    body.model_dump_json(),
                ),
            )
        return JSONResponse(terminal, headers={"Cache-Control": "no-store"})

    @router.get("/api/runtime/status")
    def runtime_status(response: Response, owner=owner_dependency):
        response.headers["Cache-Control"] = "no-store"
        try:
            with agent.ledger._connect() as connection:
                approved = budget_ready(connection, owner, runtime)
                rows = connection.execute(
                    "SELECT call_cap,used_calls,money_cap,used_money FROM budgets "
                    "WHERE version=? AND scope IN (?, '*')",
                    (runtime.budget_version if runtime else "", owner),
                ).fetchall()
        except BudgetDenied:
            report = readiness_report(settings, runtime, False)
            report["budget_status"] = "unavailable"
            return report
        report = readiness_report(settings, runtime, approved)
        if len(rows) == 2:
            report["budget"] = {
                "call_cap": min(r["call_cap"] for r in rows),
                "used_calls": max(r["used_calls"] for r in rows),
                "remaining_calls": max(0, min(r["call_cap"] - r["used_calls"] for r in rows)),
                "short_window_calls": 1
                if getattr(agent.ledger, "direct_observations", False)
                else 2
                if not getattr(agent.ledger, "allow_repairs", True)
                else 3,
                "visual_search_calls": 1 if getattr(agent.ledger, "visual_search", False) else None,
            }
        return report

    def run_job(video, owner, generation, analysis=False):
        directory = ws.root / video
        try:
            row = ws.owned("videos", video, owner)
            if row["generation"] != generation:
                raise HTTPException(409, "stale_generation")
            if analysis:
                agent.ingest(video, owner, expected_generation=generation)
            else:
                with ws.db() as c:
                    c.execute(
                        "UPDATE knowledge_jobs SET state='waiting_probe',error=NULL,updated=? "
                        "WHERE video=? AND generation=? AND state='failed'",
                        (time.time(), video, generation),
                    )
                ws.stage(video, owner, generation, "probing", lease=time.time() + 180)
                metadata = scene_windows(directory, probe(directory), settings.scene_threshold)
                previews = frames(
                    directory,
                    video,
                    metadata,
                    {"start": 0.0, "end": metadata["duration"]},
                    8,
                    "preview",
                )
                metadata["preview"] = previews
                with ws.db() as c:
                    c.execute(
                        "UPDATE videos SET metadata=? WHERE id=? AND generation=? AND deleted=0",
                        (json.dumps(metadata), video, generation),
                    )
                ws.stage(
                    video,
                    owner,
                    generation,
                    "ready" if local_configured() else "awaiting_configuration",
                    total=len(metadata["windows"]),
                    error=None if local_configured() else "real_blocked",
                    lease=None,
                )
                with ws.db() as c:
                    c.execute(
                        "UPDATE knowledge_jobs SET state='waiting_processor',total=?,updated=? "
                        "WHERE video=? AND generation=? AND state='waiting_probe'",
                        (math.ceil(metadata["duration"] / 8), time.time(), video, generation),
                    )
                if knowledge_enabled():
                    try:
                        if knowledge_jobs.processor():
                            knowledge_jobs.start(video, owner)
                    except (HTTPException, OSError, ValueError):
                        # Playback preflight remains valid if indexing cannot start.
                        pass
        except (
            MediaError,
            AgentError,
            BudgetDenied,
            HTTPException,
            KeyError,
            ValueError,
            TypeError,
            IndexError,
            OSError,
        ):
            try:
                import sys

                error = sys.exception()
                code = (
                    str(error) if isinstance(error, (MediaError, AgentError)) else "analysis_failed"
                )
                ws.stage(video, owner, generation, "failed", error=code[:100], lease=None)
                with ws.db() as c:
                    c.execute(
                        "UPDATE knowledge_jobs SET state='failed',error=?,updated=? "
                        "WHERE video=? AND generation=? AND state='waiting_probe'",
                        (code[:100], time.time(), video, generation),
                    )
            except HTTPException:
                pass
        finally:
            with worker_lock:
                workers.discard(video)

    def dispatch(video, owner, analysis=False):
        with worker_lock:
            if video in workers:
                raise HTTPException(409, "job_running")
            if len(workers) >= 2:
                raise HTTPException(429, "worker_capacity")
            row = ws.owned("videos", video, owner)
            with ws.db() as c:
                lease = c.execute(
                    "UPDATE jobs SET lease=?,state='queued',error=NULL WHERE video=? "
                    "AND generation=? AND (lease IS NULL OR lease<?) "
                    "AND EXISTS(SELECT 1 FROM videos WHERE id=? AND generation=? "
                    "AND deleted=0 AND expires>?)",
                    (
                        time.time() + 3600,
                        video,
                        row["generation"],
                        time.time(),
                        video,
                        row["generation"],
                        time.time(),
                    ),
                )
                if lease.rowcount != 1:
                    raise HTTPException(409, "job_running")
            workers.add(video)
        threading.Thread(
            target=run_job, args=(video, owner, row["generation"], analysis), daemon=True
        ).start()

    @router.get("/api/session")
    def session(request: Request, response: Response):
        existing_token = request.cookies.get("fs_session")
        row, token = ws.session(existing_token)
        cookie_token = token or existing_token
        if cookie_token:
            response.set_cookie(
                "fs_session",
                cookie_token,
                httponly=True,
                samesite="strict",
                secure=bool(os.environ.get("FS_PUBLIC_ORIGIN")) or request.url.scheme == "https",
                max_age=max(1, math.ceil(row["expires"] - time.time())),
            )
        response.headers["Cache-Control"] = "no-store"
        blockers = runtime.blockers() if runtime else ["model_endpoints_missing"]
        if settings.mode != "real":
            blockers.append("fixture_mode_no_model_requests")
        if runtime and not blockers:
            try:
                agent.ledger.admit_cost(
                    row["id"], runtime.budget_version, 1, 1, runtime.currency_unit
                )
            except BudgetDenied:
                blockers.append("budget_not_approved_or_insufficient")
        else:
            blockers.append("budget_not_approved")
        return {
            "csrf": row["csrf"],
            "owner_id": row["id"],
            "expires": row["expires"],
            "real_ready": not blockers,
            "local_vision_ready": bool(local_configured()),
            "cloud_vision_ready": cloud_vision.configured(),
            "query_reference_snapshots": True,
            "provider": runtime.provider if runtime else "nvidia_nebius",
            "blockers": sorted(set(blockers)),
            "ttl_hours": settings.ttl_hours,
        }

    @router.get("/api/investigations")
    def investigations(owner=owner_dependency):
        return {"items": ws.list("investigations", owner)}

    @router.post("/api/investigations", status_code=201)
    def new_investigation(body: NewInvestigation, owner=owner_dependency):
        return ws.create(owner, body.video_id, body.title)

    def stored_query(query, desk, owner):
        if not isinstance(query, dict) or not isinstance(query.get("id"), str):
            raise ValueError("invalid_query_reference")
        if "response" not in query and set(query) - {"id", "parentTurn"}:
            raise ValueError("invalid_query_reference")
        with ws.db() as c:
            row = c.execute(
                "SELECT result FROM queries WHERE id=? AND owner=? "
                "AND investigation=? AND state='done'",
                (query["id"], owner, desk["id"]),
            ).fetchone()
        if not row:
            raise ValueError("foreign_or_missing_query_reference")
        saved = json.loads(row["result"])
        saved.setdefault("video_id", desk["video"])
        if "parentTurn" in query:
            parent = query["parentTurn"]
            if parent is not None and not re.fullmatch(r"task-[0-9]{1,9}", str(parent)):
                raise ValueError("invalid_parent_turn")
            saved["parentTurn"] = parent
        return saved

    def expanded_investigation(desk, owner):
        if desk["video"] != "demo_parcel":
            try:
                for record in [*desk["state"].get("records", []), *desk["state"].get("pins", [])]:
                    if "response" not in record["query"]:
                        record["query"] = stored_query(record["query"], desk, owner)
            except (ValueError, KeyError, TypeError):
                raise HTTPException(409, "stored_query_unavailable") from None
        return desk

    @router.get("/api/investigations/{key}")
    def investigation(key: str, owner=owner_dependency):
        return expanded_investigation(ws.get(key, owner), owner)

    @router.put("/api/investigations/{key}")
    def save_investigation(key: str, body: SaveInvestigation, owner=owner_dependency):
        desk = ws.owned("investigations", key, owner)
        state = body.state
        if set(state) - {
            "epoch",
            "title",
            "groupTitle",
            "note",
            "records",
            "selectedId",
            "pins",
            "local",
            "inspectorTab",
            "sourceInfo",
            "canvas",
            "ui",
        }:
            raise HTTPException(422, "invalid_snapshot")
        if type(state.get("epoch", 0)) is not int or not 0 <= state.get("epoch", 0) <= 2**53 - 1:
            raise HTTPException(422, "invalid_snapshot")
        try:
            if not isinstance(state.get("records", []), list) or not isinstance(
                state.get("pins", []), list
            ):
                raise ValueError
            if len(state.get("records", [])) > 100 or len(state.get("pins", [])) > 8:
                raise HTTPException(422, "snapshot_limit")
            for record in [*state.get("records", []), *state.get("pins", [])]:
                if not re.fullmatch(r"(?:task|finding)-[0-9]{1,9}", record["id"]):
                    raise ValueError
                query = record["query"]
                if desk["video"] != "demo_parcel" and "response" not in query:
                    query = stored_query(query, desk, owner)
                if not isinstance(query["question"], str) or len(query["question"]) > 4000:
                    raise ValueError
                answer = Answer.model_validate(query["response"])
                if any(event.video_id != desk["video"] for event in answer.events):
                    raise ValueError
                if record.get("kind") == "evidence" and not any(
                    frame.model_dump(mode="json") == record.get("frame")
                    for event in answer.events
                    for frame in event.evidence
                ):
                    raise ValueError
                if desk["video"] == "demo_parcel":
                    canonical = next(
                        (q for q in catalog()["queries"] if q["id"] == query["id"]), None
                    )
                    if (
                        not canonical
                        or canonical["response"] != query["response"]
                        or query.get("mode") == "real"
                    ):
                        raise ValueError
                else:
                    original = stored_query(query, desk, owner)
                    if original["response"] != query["response"]:
                        raise ValueError
                    record["query"] = {"id": query["id"]}
                    if "parentTurn" in query:
                        record["query"]["parentTurn"] = query["parentTurn"]
            # Spatial state is plain finite JSON; never stored HTML or executable code.
            encoded = json.dumps(state, allow_nan=False)
            if len(encoded.encode()) > 1024**2:
                raise HTTPException(413, "investigation_too_large")
            if not isinstance(state.get("title", ""), str) or not isinstance(
                state.get("note", ""), str
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "invalid_snapshot") from None
        return expanded_investigation(ws.save(key, owner, body.revision, state), owner)

    @router.delete("/api/investigations/{key}", status_code=204)
    def remove_investigation(key: str, owner=owner_dependency):
        ws.delete("investigations", key, owner)

    @router.get("/api/videos")
    def videos(owner=owner_dependency):
        # A list can otherwise combine metadata from before preflight finished
        # with a job or index status from after it finished.
        with ws.db() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM videos WHERE owner=? AND deleted=0 AND expires>?",
                (owner, time.time()),
            ).fetchall()
            items = []
            for raw in rows:
                row = dict(raw)
                job = connection.execute(
                    "SELECT * FROM jobs WHERE video=? ORDER BY updated DESC LIMIT 1", (row["id"],)
                ).fetchone()
                status = knowledge_status(row["id"], owner, source=row, connection=connection)
                item = {k: v for k, v in row.items() if k not in {"state", "owner", "manifest"}}
                item["metadata"] = json.loads(row["metadata"])
                item["job"] = dict(job) if job else None
                item["knowledge_status"] = status
                items.append(item)
        return {
            "items": sorted(
                items, key=lambda item: item.get("updated", item["expires"]), reverse=True
            )
        }

    @router.post("/api/videos", status_code=201)
    async def upload(request: Request, owner=owner_dependency):
        name = request.headers.get("x-file-name", "video")
        from urllib.parse import unquote

        name = Path(unquote(name).replace("\\", "/")).name[:180] or "video"
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("video/") and content_type != "application/octet-stream":
            raise HTTPException(415, "video_required")
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "invalid_upload_length") from None
        if size > MAX_UPLOAD:
            raise HTTPException(413, "upload_limit")
        async with upload_gate:
            video, job = ident("vid_"), ident("job_")
            directory = ws.root / video
            with ws.db() as c:
                c.execute("BEGIN IMMEDIATE")
                used = c.execute(
                    "SELECT count(*),coalesce(sum(bytes),0) FROM videos "
                    "WHERE owner=? AND deleted=0 AND expires>?",
                    (owner, time.time()),
                ).fetchone()
                total = c.execute(
                    "SELECT coalesce(sum(bytes),0) FROM videos WHERE deleted=0 AND expires>?",
                    (time.time(),),
                ).fetchone()[0]
                if (
                    used[0] >= 10
                    or used[1] + MAX_UPLOAD > 1024**3
                    or total + MAX_UPLOAD > 4 * 1024**3
                ):
                    raise HTTPException(429, "storage_limit")
                c.execute(
                    "INSERT INTO videos(id,owner,name,bytes,expires) VALUES(?,?,?,?,?)",
                    (video, owner, name, MAX_UPLOAD, time.time() + ws.ttl),
                )
            directory.mkdir(mode=0o700)
            received = 0
            try:
                with (directory / "original").open("wb") as f:
                    async with asyncio.timeout(180):
                        async for chunk in request.stream():
                            received += len(chunk)
                            if received > MAX_UPLOAD:
                                raise HTTPException(413, "upload_limit")
                            f.write(chunk)
                if not received:
                    raise HTTPException(400, "empty_upload")
                with ws.db() as c:
                    c.execute("UPDATE videos SET bytes=? WHERE id=?", (received, video))
                    c.execute(
                        "INSERT INTO jobs(id,video,generation,state,updated) "
                        "VALUES(?,?,1,'queued',?)",
                        (job, video, time.time()),
                    )
                    c.execute(
                        "INSERT INTO knowledge_jobs(video,generation,state,updated) "
                        "VALUES(?,1,'waiting_probe',?)",
                        (video, time.time()),
                    )
                dispatch(video, owner)
            except BaseException:
                with ws.db() as c:
                    c.execute("UPDATE videos SET deleted=1 WHERE id=?", (video,))
                shutil.rmtree(directory, ignore_errors=True)
                raise
            return {"id": video, "name": name, "bytes": received, "job": ws.job(video, owner)}

    @router.get("/api/videos/{video}")
    def video_detail(video: str, owner=owner_dependency):
        # Video metadata, preflight state and knowledge state must reflect the
        # same SQLite snapshot; the worker can finish between independent reads.
        with ws.db() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM videos WHERE id=? AND owner=?", (video, owner)
            ).fetchone()
            if not row:
                raise HTTPException(404, "not_found")
            row = dict(row)
            if row["deleted"] or row["expires"] <= time.time():
                raise HTTPException(410, "expired_or_deleted")
            job = connection.execute(
                "SELECT * FROM jobs WHERE video=? ORDER BY updated DESC LIMIT 1", (video,)
            ).fetchone()
            status = knowledge_status(video, owner, source=row, connection=connection)
        return {
            "id": video,
            "name": row["name"],
            "metadata": json.loads(row["metadata"]),
            "generation": row["generation"],
            "job": dict(job) if job else None,
            "knowledge_status": status,
            "expires": row["expires"],
            "capabilities": json.loads(row["manifest"])["capabilities"]
            if row["manifest"]
            else None,
        }

    @router.get("/api/videos/{video}/knowledge")
    def get_knowledge_status(video: str, owner=owner_dependency):
        return knowledge_status(video, owner)

    @router.post("/api/videos/{video}/knowledge/build", status_code=202)
    def build_knowledge(video: str, owner=owner_dependency):
        ws.owned("videos", video, owner)
        if not knowledge_enabled():
            raise HTTPException(409, "knowledge_processor_unavailable")
        knowledge_jobs.start(video, owner)
        return knowledge_status(video, owner)

    @router.post("/api/videos/{video}/knowledge/cancel", status_code=202)
    def cancel_knowledge(video: str, owner=owner_dependency):
        ws.owned("videos", video, owner)
        if not knowledge_enabled():
            raise HTTPException(409, "knowledge_processor_unavailable")
        knowledge_jobs.cancel(video, owner)
        return knowledge_status(video, owner)

    @router.patch("/api/videos/{video}")
    def rename_video(video: str, body: RenameVideo, owner=owner_dependency):
        ws.owned("videos", video, owner)
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name_required")
        with ws.db() as c:
            c.execute(
                "UPDATE videos SET name=? WHERE id=? AND owner=? AND deleted=0",
                (name, video, owner),
            )
        return {"id": video, "name": name}

    @router.post("/api/videos/{video}/analyze", status_code=202)
    def analyze(video: str, owner=owner_dependency):
        row = ws.owned("videos", video, owner)
        job = ws.job(video, owner)
        if settings.mode != "real" or not runtime or runtime.blockers():
            raise HTTPException(409, "real_blocked")
        if not json.loads(row["metadata"]).get("windows"):
            raise HTTPException(409, "preflight_required")
        if job["state"] == "ready":
            return job
        if job["error"] in {"unknown", "model_unknown", "unknown_attempt_do_not_resend"}:
            raise HTTPException(409, "unknown_attempt_do_not_resend")
        try:
            agent.admit_ingest(video, owner)
        except BudgetDenied:
            raise HTTPException(409, "budget_not_approved_or_insufficient") from None
        except AgentError as error:
            raise HTTPException(409, str(error)) from None
        dispatch(video, owner, True)
        return ws.job(video, owner)

    @router.post("/api/videos/{video}/retry", status_code=202)
    def retry_probe(video: str, owner=owner_dependency):
        row = ws.owned("videos", video, owner)
        if ws.job(video, owner)["state"] not in {"failed", "cancelled"} or row["manifest"]:
            raise HTTPException(409, "retry_not_available")
        if json.loads(row["metadata"]).get("windows"):
            raise HTTPException(409, "use_analyze_for_model_retry")
        dispatch(video, owner)
        return ws.job(video, owner)

    @router.post("/api/videos/{video}/cancel", status_code=202)
    def cancel(video: str, owner=owner_dependency):
        ws.owned("videos", video, owner)
        with ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE videos SET generation=generation+1,manifest=NULL WHERE id=?", (video,)
            )
            c.execute(
                "UPDATE jobs SET state='cancelled',error='cancelled',lease=NULL,"
                "generation=(SELECT generation FROM videos WHERE id=?),updated=? WHERE video=?",
                (video, time.time(), video),
            )
        return ws.job(video, owner)

    @router.delete("/api/videos/{video}", status_code=204)
    def delete_video(video: str, owner=owner_dependency):
        ws.delete("videos", video, owner)
        ws.cleanup()

    @router.get("/media/{video}/video")
    def private_video(video: str, owner=owner_dependency):
        ws.owned("videos", video, owner)
        path = ws.root / video / "original"
        # Originals have no extension; do not let that select a generic MIME type.
        # Inspect only the bounded container header, not the mutable display name.
        with path.open("rb") as source:
            header = source.read(64)
        media_type = "application/octet-stream"
        if header[4:8] == b"ftyp":
            media_type = "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
        elif header[:4] == b"\x1aE\xdf\xa3":
            media_type = "video/webm" if b"webm" in header else "video/x-matroska"
        elif header[:4] == b"RIFF" and header[8:12] == b"AVI ":
            media_type = "video/x-msvideo"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.get("/media/{video}/frames/{file}")
    def private_frame(video: str, file: str, owner=owner_dependency):
        ws.owned("videos", video, owner)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.jpg", file):
            raise HTTPException(404, "not_found")
        path = ws.root / video / "frames" / file
        if not path.is_file():
            raise HTTPException(404, "not_found")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def prior(body, owner, desk):
        if body.parent_request_id is None:
            return None
        with ws.db() as c:
            row = c.execute(
                "SELECT * FROM queries WHERE owner=? AND id=? AND investigation=? "
                "AND generation=? AND state='done'",
                (owner, body.parent_request_id, desk["id"], body.generation),
            ).fetchone()
        if not row:
            raise HTTPException(409, "invalid_context_anchor")
        result = json.loads(row["result"])
        if not result["response"]["events"]:
            raise HTTPException(409, "context_has_no_verified_anchor")
        return {"question": result["question"], "events": result["response"]["events"]}

    @router.get("/api/investigations/{key}/requests")
    def query_requests(key: str, owner=owner_dependency):
        desk = ws.owned("investigations", key, owner)
        generation = json.loads(desk["state"]).get("epoch", 0)
        with ws.db() as c:
            rows = c.execute(
                "SELECT * FROM queries WHERE investigation=? AND owner=? "
                "AND generation=? ORDER BY updated DESC LIMIT 100",
                (key, owner, generation),
            ).fetchall()
            supplements = c.execute(
                "SELECT request_id,state,input FROM knowledge_supplement_jobs s "
                "WHERE investigation=? AND owner=? AND desk_epoch=? AND s.video=? "
                "AND EXISTS (SELECT 1 FROM videos v WHERE v.id=s.video AND v.owner=s.owner "
                "AND v.generation=s.generation AND v.deleted=0 AND v.expires>?) "
                "AND state IN ('running','unknown','done','failed','cancelled','waiting_budget') "
                "AND NOT EXISTS "
                "(SELECT 1 FROM queries q WHERE q.id=s.request_id AND q.owner=s.owner) "
                "ORDER BY updated DESC LIMIT 100",
                (key, owner, generation, desk["video"], time.time()),
            ).fetchall()
        return {
            "items": [
                {
                    "id": r["id"],
                    "state": r["state"],
                    "input": json.loads(r["input"]) if r["input"] else None,
                    "result": json.loads(r["result"]) if r["result"] else None,
                }
                for r in rows
            ],
            "supplements": [
                {
                    "id": r["request_id"],
                    "state": r["state"],
                    "input": json.loads(r["input"]) if r["input"] else None,
                }
                for r in supplements
            ],
        }

    def prepare(body: ChatInput, owner, check_budget=True):
        desk = ws.owned("investigations", body.investigation_id, owner)
        state = json.loads(desk["state"])
        if body.generation != state.get("epoch", 0):
            raise HTTPException(409, "stale_chat_generation")
        with ws.db() as connection:
            existing = connection.execute(
                "SELECT hash FROM queries WHERE id=? AND owner=?",
                (body.request_id, owner),
            ).fetchone()
        if existing:
            if existing["hash"] != digest(body.model_dump_json()):
                raise HTTPException(409, "request_payload_conflict")
            # Restoring an existing pending/unknown/terminal request must not need
            # a fresh budget or trigger another model dispatch.
            return {"kind": "ready", "request_id": body.request_id}
        if desk["video"] != "demo_parcel" and (
            settings.mode != "real" or not runtime or runtime.blockers()
        ):
            return {
                "kind": "runtime_blocked",
                "message": "当前服务未启用真实模型，或模型配置未就绪。"
                "已保存的回答可以回看，但本次问题尚未发送给模型。"
                "原问题已保留；请在「更多 → 模型与预算」查看配置。",
            }
        context = prior(body, owner, desk)
        from frameseek.visual_search import requires_aggregation

        if requires_aggregation(body.question) and not re.search(
            r"这[个张幅]画面|这张图", body.question
        ):
            try:
                source = (
                    ws.owned("videos", desk["video"], owner)
                    if desk["video"] != "demo_parcel"
                    else None
                )
                duration = json.loads(source["metadata"]).get("duration") if source else 18
                short_scope = explicit_time_window(body.question, duration) if duration else None
            except AgentError:
                short_scope = None
        else:
            short_scope = True
        if not short_scope:
            return {
                "kind": "capability_blocked",
                "message": "尚未完成全片连续事件和唯一实体追踪，不能从索引抽样推算总次数或排名。"
                "可以查找相关人物、物品或动作，列出已观察到的时间和证据；"
                "上传片段也不能代表未出现在素材里的完整活动。"
                "本次尚未调用模型，输入已保留。",
            }
        if re.fullmatch(
            r"\s*(?:请)?(?:继续|接着)(?:这个|刚才的|上个|上一[轮个])?(?:问题|提问)?[。！？?！\s]*",
            body.question,
        ):
            return {
                "kind": "clarification_required",
                "message": "请补充这次想核对的具体内容，例如「这张画面的比赛还剩多少秒？」。"
                "本次尚未调用模型，输入已保留。",
            }
        if (
            re.search(r"^(它|他|她|那里|那之后|然后呢|后来呢|中间呢|那个)", body.question)
            and not context
        ):
            return {
                "kind": "clarification_required",
                "message": "请选中有证据的问题，或明确要查找的物体与时间。",
            }
        if check_budget and desk["video"] != "demo_parcel":
            video = ws.owned("videos", desk["video"], owner)
            if video["manifest"]:
                manifest = json.loads(video["manifest"])
                try:
                    _, calls, cost = agent.query_budget(
                        manifest, json.loads(video["metadata"]), body.question, context
                    )
                except AgentError as error:
                    if str(error) != "invalid_time_window":
                        raise
                    return {
                        "kind": "clarification_required",
                        "message": "视频时间段无效：开始时间须早于结束时间，且都在视频长度以内。"
                        "本次尚未调用模型，输入已保留。",
                    }
                try:
                    agent.ledger.admit_cost(
                        owner, runtime.budget_version, calls, cost, runtime.currency_unit
                    )
                except BudgetDenied:
                    try:
                        with agent.ledger._connect() as connection:
                            rows = connection.execute(
                                "SELECT call_cap-used_calls FROM budgets "
                                "WHERE version=? AND scope IN (?, '*')",
                                (runtime.budget_version, owner),
                            ).fetchall()
                    except BudgetDenied:
                        return {
                            "kind": "budget_unavailable",
                            "message": "无法读取当前预算账本，本次尚未发送给模型。"
                            "输入已保留；不会自动重试或启用其他额度。",
                        }
                    remaining = min((row[0] for row in rows), default=0)
                    return {
                        "kind": "budget_blocked",
                        "message": f"本问题的处理路径需预留最多{calls}次模型请求，"
                        f"目前最多剩{remaining}次；"
                        "也可能因金额或审批状态未通过准入。尚未调用模型，输入已保留。"
                        "可指定不超过60秒的具体视频时间段以减少检索调用。",
                    }
        return {"kind": "ready", "request_id": body.request_id}

    @router.post("/api/auto-chat/prepare")
    def prepare_auto_chat(body: ChatInput, owner=owner_dependency):
        from frameseek.auto_routing import choose_engine, needs_audio

        # Same owned source/generation/context validation as index chat, before any
        # capability inspection. This endpoint never dispatches or writes a query.
        desk, source, question = index_chat_input(body, owner, allow_model_context=True)
        with ws.db() as connection:
            existing = connection.execute(
                "SELECT id FROM queries WHERE id=? AND owner=?", (body.request_id, owner)
            ).fetchone()
        if existing:
            return {
                "kind": "request_exists",
                "engine": "model",
                "model_requests_sent": 0,
                "message": "这条请求已存在，请恢复原请求；不会重新选择模型或重发。",
            }
        if knowledge_enabled():
            status = knowledge_status(source["id"], owner)
            if status["state"] != "ready" or not status["version"]:
                return {
                    "kind": "knowledge_not_ready",
                    "plan_kind": "index_not_ready",
                    "engine": "index",
                    "knowledge_status": status,
                    "model_requests_sent": 0,
                    "message": "基础索引尚未完成；可继续播放视频和查看已有回答。",
                }
        if not knowledge_enabled() and ws.job(source["id"], owner)["state"] != "ready":
            raise HTTPException(409, "source_not_ready")
        if body.parent_request_id:
            with ws.db() as connection:
                saved = json.loads(
                    connection.execute(
                        "SELECT result FROM queries WHERE owner=? AND id=? AND investigation=?",
                        (owner, body.parent_request_id, desk["id"]),
                    ).fetchone()["result"]
                )
            if saved.get("video_id") != source["id"]:
                raise HTTPException(409, "foreign_context_video")
            if saved.get("source_generation", source["generation"]) != source["generation"]:
                raise HTTPException(409, "stale_context_generation")
        model_continuation = False
        if body.parent_request_id:
            from frameseek.question_context import resolve_followup, saved_question

            def load_ancestor(key):
                with ws.db() as connection:
                    row = connection.execute(
                        "SELECT result,input FROM queries WHERE owner=? AND investigation=? "
                        "AND id=? AND generation=? AND state='done'",
                        (owner, desk["id"], key, body.generation),
                    ).fetchone()
                return (
                    (json.loads(row["result"]), json.loads(row["input"] or "{}")) if row else None
                )

            first = load_ancestor(body.parent_request_id)
            if first is None:
                raise HTTPException(409, "invalid_context_anchor")
            try:
                original = saved_question(
                    saved,
                    first[1],
                    load_ancestor,
                    video=source["id"],
                    generation=source["generation"],
                )
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            resolved = resolve_followup(question, {"question": original})
            model_continuation = resolved != question and saved.get("engine") != "index"
            question = resolved
        if knowledge_enabled():
            if needs_audio(question):
                return {
                    "kind": "capability_blocked",
                    "plan_kind": "capability_missing",
                    "engine": "index",
                    "message": "当前基础索引没有语音转写，无法根据画面判断声音内容。",
                    "model_requests_sent": 0,
                }

            def supplement_response(plan, message):
                if settings.mode == "fixture" and knowledge_jobs.processor() is None:
                    if result["kind"] == "indexed_occurrences":
                        return {
                            "kind": "ready",
                            "engine": "index",
                            "plan_kind": "direct_retrieval",
                            "route_message": "本机补查暂不可用，将显示已有索引及覆盖限制。",
                            "knowledge_version": result["knowledge_version"],
                            "model_requests_sent": 0,
                        }
                    return {
                        **result,
                        "engine": "index",
                        "plan_kind": "processor_unavailable",
                        "message": message + " 本机补查暂不可用。",
                        "model_requests_sent": 0,
                    }
                response = {
                    "kind": "supplement_required",
                    "plan_kind": "bounded_supplement",
                    "engine": "index",
                    "message": message,
                    "supplement_plan": plan,
                    "model_requests_sent": 0,
                    "has_index_matches": bool(
                        result.get("kind") == "indexed_occurrences"
                        and result.get("total_occurrences")
                    ),
                }
                if settings.mode == "real":
                    try:
                        estimate = knowledge_jobs.preview_cloud_supplement(
                            source, owner, question, plan
                        )
                    except BudgetDenied as error:
                        unknown = "unknown" in str(error).lower()
                        return {
                            **response,
                            "kind": "supplement_unknown" if unknown else "budget_blocked",
                            "message": (
                                "之前的补查调用结果不确定；不会自动重发，原问题已保留。"
                                if unknown
                                else "补查的配置或预算未通过；本次未调用模型，原问题已保留。"
                            ),
                        }
                    except (ValueError, KeyError, TypeError, OSError):
                        raise HTTPException(409, "observation_index_unavailable_or_stale") from None
                    response["estimated_new_calls"] = estimate["new_calls"]
                    response["estimated_max_cost_units"] = estimate["maximum_cost_units"]
                    response["currency_unit"] = runtime.currency_unit
                return response

            result = indexed_search(source, question, limit=1)
            from frameseek.question_context import CONTINUATION, PRONOUN_TIME

            reuse_parent = bool(
                body.parent_request_id
                and (
                    PRONOUN_TIME.fullmatch(body.question)
                    or CONTINUATION.fullmatch(body.question)
                    or re.fullmatch(
                        r"\s*(?:还有吗|再找一次|同样条件|same criteria|again)[？?。\s]*",
                        body.question,
                        re.I,
                    )
                )
                and saved.get("engine") == "index"
                and (saved.get("index_search") or {}).get("knowledge_version")
                == result.get("knowledge_version")
            )
            # Repeating the same question against the unchanged published
            # version must reuse its completed supplement. Match the original
            # parent as well: the same words may mean something different in
            # another conversation context. Never reuse cancelled/unknown work.
            previous_supplement = completed_question_supplement(
                body, owner, desk, source, result.get("knowledge_version")
            )
            reuse_exact = bool(previous_supplement)
            if result["kind"] == "clarification_required":
                if reuse_exact:
                    return {
                        **result,
                        "kind": "ready",
                        "engine": "index",
                        "plan_kind": "supplement_unconfirmed",
                        "route_message": "补查已完成，正在整理未确认结果。",
                        "model_requests_sent": 0,
                    }
                from frameseek.knowledge_planning import plan_supplement

                try:
                    index, version = current_knowledge(source)
                    if version != result.get("knowledge_version"):
                        raise ValueError("knowledge_version_changed")
                    plan = plan_supplement(index, question, version)
                except (ValueError, KeyError, TypeError, OSError):
                    raise HTTPException(409, "observation_index_unavailable_or_stale") from None
                message = result.get("message", "现有索引无法确认这个问题。")
                if plan["kind"] == "supplement_candidates":
                    return supplement_response(
                        plan, message + " 已定位相关片段，可以进行局部补查。"
                    )
                elif plan["kind"] == "coverage_gap":
                    message += " 没有可定位的候选片段，不会自动重扫全片。"
                return {
                    **result,
                    "kind": (
                        "supplement_required"
                        if plan["kind"] == "supplement_candidates"
                        else result["kind"]
                    ),
                    "engine": "index",
                    "message": message,
                    "supplement_plan": plan,
                    "plan_kind": (
                        "bounded_supplement"
                        if plan["kind"] == "supplement_candidates"
                        else plan["kind"]
                    ),
                    "model_requests_sent": 0,
                }
            coverage_scope = result.get("coverage", {}).get("time_scope")
            short_scope = bool(
                coverage_scope
                and math.ceil(coverage_scope["end"] / 8) - math.floor(coverage_scope["start"] / 8)
                <= 2
            )
            # Quoted video text is data, not evidence of search intent. For
            # example, an existence question about the word "when" must not
            # initiate a time-listing supplement just because of that literal.
            intent_text = re.sub(r'“[^”]*”|"[^"\n]*"|‘[^’]*’', " ", question)
            if (
                result.get("kind") == "indexed_occurrences"
                and not (reuse_parent or reuse_exact)
                and re.search(
                    r"找出|找到|列出|出现时间|出现在哪|什么时候|何时|全部|所有|"
                    r"\bfind\b|\blist\b|\bshow\b|\bwhen\b|\bwhere\b",
                    intent_text,
                    re.I,
                )
                and not (
                    result.get("total_occurrences")
                    and not short_scope
                    and any(
                        condition.get("field") == "text" and condition.get("operator") == "contains"
                        for condition in result.get("conditions", [])
                    )
                )
            ):
                with ws.db() as c:
                    prior_supplement = c.execute(
                        "SELECT state,version FROM knowledge_supplement_jobs "
                        "WHERE owner=? AND investigation=? AND request_id=?",
                        (owner, desk["id"], body.request_id),
                    ).fetchone()
                if not (
                    prior_supplement
                    and prior_supplement["state"] == "done"
                    and prior_supplement["version"] == result.get("knowledge_version")
                ):
                    from frameseek.knowledge_planning import plan_supplement

                    try:
                        index, version = current_knowledge(source)
                        if version != result.get("knowledge_version"):
                            raise ValueError("knowledge_version_changed")
                        plan = plan_supplement(index, question, version)
                    except (ValueError, KeyError, TypeError, OSError):
                        raise HTTPException(409, "observation_index_unavailable_or_stale") from None
                    if plan["kind"] == "supplement_candidates":
                        return supplement_response(plan, "部分画面仍不确定，将补查相关片段。")
            return {
                "kind": "ready",
                "engine": "index",
                "plan_kind": "direct_retrieval",
                "route_message": "正在检索已建立的基础索引；抽帧可能遗漏短暂内容。",
                "knowledge_version": result["knowledge_version"],
                "model_requests_sent": 0,
            }
        result = {}
        if not needs_audio(question) and not model_continuation:
            try:
                result = indexed_search(source, question, limit=1)
            except HTTPException as error:
                if (
                    error.status_code != 409
                    or error.detail != "observation_index_unavailable_or_stale"
                ):
                    raise
        engine, message = choose_engine(
            question,
            result,
            local_ready=bool(local_configured()),
            cloud_ready=cloud_vision.configured(),
        )
        if engine == "index":
            admission = {"kind": "ready"}
        elif engine == "local":
            admission = {"kind": "ready"}
        elif engine == "cloud":
            admission = cloud_vision.prepare(source, owner, question)
        else:
            admission = prepare(body, owner)
        return {**admission, "engine": engine, "route_message": message, "model_requests_sent": 0}

    @router.post("/api/chat/prepare")
    def prepare_chat(body: ChatInput, owner=owner_dependency):
        require_unified_chat(ws.owned("investigations", body.investigation_id, owner))
        return prepare(body, owner)

    @router.post("/api/knowledge-chat/supplement", status_code=202)
    def start_knowledge_supplement(body: ChatInput, owner=owner_dependency):
        if not knowledge_enabled():
            raise HTTPException(409, "knowledge_processor_unavailable")
        desk, source, question = index_chat_input(body, owner)
        if body.parent_request_id:
            with ws.db() as c:
                row = c.execute(
                    "SELECT result FROM queries WHERE owner=? AND investigation=? AND id=? "
                    "AND generation=? AND state='done'",
                    (owner, desk["id"], body.parent_request_id, body.generation),
                ).fetchone()
            if not row:
                raise HTTPException(409, "invalid_context_anchor")
            parent = json.loads(row["result"])
            if parent.get("video_id") != source["id"]:
                raise HTTPException(409, "foreign_context_video")
            if parent.get("source_generation", source["generation"]) != source["generation"]:
                raise HTTPException(409, "stale_context_generation")
        with ws.db() as c:
            previous = c.execute(
                "SELECT * FROM knowledge_supplement_jobs WHERE owner=? AND request_id=?",
                (owner, body.request_id),
            ).fetchone()
        if previous:
            if previous["payload_hash"] != digest(body.model_dump_json()) or (
                previous["investigation"] != desk["id"]
            ):
                raise HTTPException(409, "request_payload_conflict")
            return {
                "state": previous["state"],
                "version": previous["version"],
                "error": previous["error"],
            }
        from frameseek.knowledge_planning import plan_supplement

        try:
            index, version = current_knowledge(source)
            plan = plan_supplement(index, question, version)
        except (ValueError, KeyError, TypeError, OSError):
            raise HTTPException(409, "observation_index_unavailable_or_stale") from None
        if plan["kind"] != "supplement_candidates":
            raise HTTPException(409, "knowledge_no_supplement_candidates")
        return knowledge_jobs.start_supplement(source, desk, owner, body, question, plan)

    @router.get("/api/investigations/{key}/supplements/{request_id}")
    def knowledge_supplement_status(key: str, request_id: Key, owner=owner_dependency):
        return knowledge_jobs.supplement_status(key, owner, request_id)

    @router.post("/api/investigations/{key}/supplements/{request_id}/cancel", status_code=202)
    def cancel_knowledge_supplement(key: str, request_id: Key, owner=owner_dependency):
        return {"state": knowledge_jobs.cancel_supplement(key, owner, request_id)}

    @router.post("/api/investigations/{key}/requests/{request_id}/cancel")
    def cancel_query(key: str, request_id: Key, owner=owner_dependency):
        desk = ws.owned("investigations", key, owner)
        with ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM queries WHERE id=? AND owner=? AND investigation=?",
                (request_id, owner, key),
            ).fetchone()
            if not row:
                raise HTTPException(404, "request_not_found")
            if row["state"] == "done":
                return json.loads(row["result"])
            terminal = unknown_final(request_id)
            terminal["video_id"] = desk["video"]
            terminal["question"] = json.loads(row["input"])["question"]
            terminal["response"]["answer"] = "本轮查找已取消，未生成有效证据。"
            terminal["response"]["errors"][0].update(
                code="query_cancelled",
                message="查找已取消",
                suggested_action="如需继续，请显式提交新问题。",
            )
            progress = ws.root / desk["video"] / (request_id + "_verified.json")
            if progress.is_file():
                verified = Answer.model_validate_json(progress.read_text()).model_dump(mode="json")
                if all(event["video_id"] == desk["video"] for event in verified["events"]):
                    terminal["response"] = verified
            c.execute(
                "UPDATE queries SET state='done',result=?,http=200,updated=? "
                "WHERE id=? AND owner=?",
                (json.dumps(terminal, ensure_ascii=False), time.time(), request_id, owner),
            )
        from frameseek.local_routes import interrupt_local_query

        interrupt_local_query(owner, key, request_id)
        return terminal

    @router.post("/api/chat")
    def chat(body: ChatInput, owner=owner_dependency):
        desk = ws.owned("investigations", body.investigation_id, owner)
        require_unified_chat(desk)
        if desk["video"] == "demo_parcel":
            raise HTTPException(409, "demo_uses_explicit_fixtures")
        generation = json.loads(desk["state"]).get("epoch", 0)
        if generation != body.generation:
            raise HTTPException(409, "stale_chat_generation")
        context = prior(body, owner, desk)
        if prepare(body, owner, check_budget=False)["kind"] != "ready":
            raise HTTPException(409, "clarification_required")
        payload_hash = digest(body.model_dump_json())
        with ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM queries WHERE id=? AND owner=?", (body.request_id, owner)
            ).fetchone()
            if old:
                if old["hash"] != payload_hash:
                    raise HTTPException(409, "request_payload_conflict")
                if old["state"] == "done":
                    return JSONResponse(json.loads(old["result"]), status_code=old["http"])
                return JSONResponse(
                    {
                        "kind": "query_control",
                        "state": "pending" if old["state"] == "running" else "unknown",
                        "request_id": body.request_id,
                        "retry_after_seconds": 3 if old["state"] == "running" else None,
                        "suggested_action": "同一请求恢复结果"
                        if old["state"] == "running"
                        else "调用结果不确定，请勿自动重发",
                    },
                    status_code=202 if old["state"] == "running" else 409,
                    headers={"Retry-After": "3"} if old["state"] == "running" else None,
                )
            if (
                c.execute("SELECT count(*) FROM queries WHERE owner=?", (owner,)).fetchone()[0]
                >= 100
                or c.execute("SELECT count(*) FROM queries").fetchone()[0] >= 1000
            ):
                raise HTTPException(429, "query_history_limit")
            # A user-submitted new request explicitly ends an older unknown
            # execution; it never redispatches the old attempt ID.
            unknowns = c.execute(
                "SELECT id,input FROM queries WHERE investigation=? "
                "AND owner=? AND generation=? AND state='unknown'",
                (desk["id"], owner, generation),
            ).fetchall()
            for old_query in unknowns:
                terminal = unknown_final(old_query["id"])
                terminal["video_id"] = desk["video"]
                progress = ws.root / desk["video"] / (old_query["id"] + "_verified.json")
                if progress.is_file():
                    verified = Answer.model_validate_json(progress.read_text()).model_dump(
                        mode="json"
                    )
                    if all(event["video_id"] == desk["video"] for event in verified["events"]):
                        terminal["response"] = verified
                if old_query["input"]:
                    terminal["question"] = json.loads(old_query["input"])["question"]
                c.execute(
                    "UPDATE queries SET state='done',result=?,http=? WHERE id=? AND owner=?",
                    (
                        json.dumps(terminal),
                        200 if terminal["response"]["status"] == "partial" else 502,
                        old_query["id"],
                        owner,
                    ),
                )
            if not agent.slots.acquire(blocking=False):
                raise HTTPException(429, "query_capacity")
            c.execute(
                "INSERT INTO queries(id,investigation,owner,generation,hash,state,updated,input) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (
                    body.request_id,
                    desk["id"],
                    owner,
                    generation,
                    payload_hash,
                    time.time(),
                    body.model_dump_json(),
                ),
            )
        status = 200
        try:

            def chat_guard():
                active = ws.owned("investigations", desk["id"], owner)
                if json.loads(active["state"]).get("epoch", 0) != generation:
                    raise HTTPException(409, "stale_chat_generation")
                with ws.db() as c:
                    request = c.execute(
                        "SELECT state FROM queries WHERE id=? AND owner=?",
                        (body.request_id, owner),
                    ).fetchone()
                if not request or request["state"] != "running":
                    raise AgentError("query_cancelled")

            answer = agent.query(
                desk["video"], owner, body.question, body.request_id, context, guard=chat_guard
            )
        except (
            AgentError,
            BudgetDenied,
            ValueError,
            KeyError,
            HTTPException,
            TypeError,
            IndexError,
        ) as error:
            code = (
                "budget_blocked"
                if isinstance(error, BudgetDenied)
                else (str(error) if isinstance(error, AgentError) else "analysis_failed")
            )
            if code in {"model_unknown", "unknown_attempt_do_not_resend"}:
                with ws.db() as c:
                    c.execute(
                        "UPDATE queries SET state='unknown',updated=? WHERE id=? AND owner=? "
                        "AND state='running'",
                        (time.time(), body.request_id, owner),
                    )
                    cancelled = c.execute(
                        "SELECT state,result,http FROM queries WHERE id=? AND owner=?",
                        (body.request_id, owner),
                    ).fetchone()
                if cancelled and cancelled["state"] == "done":
                    return JSONResponse(
                        json.loads(cancelled["result"]), status_code=cancelled["http"]
                    )
                return JSONResponse(
                    {
                        "kind": "query_control",
                        "state": "unknown",
                        "request_id": body.request_id,
                        "retry_after_seconds": None,
                        "suggested_action": "调用结果不确定，不会自动重复扣费；原问题已保留。",
                    },
                    status_code=409,
                )
            status = 409 if code in {"video_not_ready", "real_blocked", "budget_blocked"} else 502
            budget_blocked = code == "budget_blocked"
            audio_unavailable = code == "audio_channel_unavailable"
            answer = {
                "schema_version": "0.1",
                "status": "error",
                "timebase": "video_absolute_seconds",
                "answer": "本轮被预算检查拦截：审批已暂停，或剩余请求次数/金额不足。"
                "未完成新的视觉复核。"
                if budget_blocked
                else "这段视频的音轨尚未完成可靠转写，无法回答解说内容。"
                "本轮未调用模型，也未产生新的模型费用。"
                if audio_unavailable
                else "本轮查找未完成，未生成有效证据。",
                "events": [],
                "unresolved": [],
                "followups": [],
                "trace": {
                    "visible": {"channels": [], "segments_reviewed": 0, "evidence_segments": 0},
                    "debug": None,
                },
                "errors": [
                    {
                        "code": code if re.fullmatch(r"[a-z_]+", code) else "analysis_failed",
                        "stage": "query",
                        "message": "预算审批已暂停或余额不足"
                        if budget_blocked
                        else "音轨尚未完成可靠转写"
                        if audio_unavailable
                        else "查找未完成",
                        "cause": "approved_budget_unavailable"
                        if budget_blocked
                        else "audio_channel_unavailable"
                        if audio_unavailable
                        else "unknown",
                        "retryable": False,
                        "suggested_action": "可以改问画面中可见的内容；目前不能依据音轨回答。"
                        if audio_unavailable
                        else "检查素材就绪状态、模型配置和剩余预算。",
                        "docs_url": "/docs/errors",
                        "request_id": body.request_id,
                    }
                ],
            }
        finally:
            agent.slots.release()
        # Reset/delete/expiry while the request ran must suppress late results.
        current = ws.owned("investigations", desk["id"], owner)
        if json.loads(current["state"]).get("epoch", 0) != generation:
            raise HTTPException(409, "stale_chat_generation")
        result = {
            "id": body.request_id,
            "question": body.question,
            "response": answer,
            "mode": "real",
            "video_id": desk["video"],
            "usedContext": bool(context),
        }
        with ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute(
                "SELECT state,result,http FROM queries WHERE id=? AND owner=?",
                (body.request_id, owner),
            ).fetchone()
            if previous and previous["state"] == "done":
                return JSONResponse(json.loads(previous["result"]), status_code=previous["http"])
            c.execute(
                "UPDATE queries SET state='done',result=?,http=?,updated=? WHERE id=? AND owner=?",
                (
                    json.dumps(result, ensure_ascii=False),
                    status,
                    time.time(),
                    body.request_id,
                    owner,
                ),
            )
        return JSONResponse(result, status_code=status)

    from frameseek.local_routes import install_local_routes

    install_local_routes(router, ws, owner_dependency, ChatInput, chat_guard=require_unified_chat)
    install_local_routes(
        router, ws, owner_dependency, ChatInput, cloud=cloud_vision, chat_guard=require_unified_chat
    )
    app.include_router(router)
    return ws
