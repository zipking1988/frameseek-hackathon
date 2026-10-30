"""Opt-in knowledge build orchestration for synthetic, local and cloud processors."""

import copy
import json
import math
import os
import threading
import time
from contextlib import contextmanager, nullcontext

from fastapi import HTTPException

from frameseek.attempts import BudgetDenied
from frameseek.index_service import source_digest
from frameseek.knowledge_compiler import compile_pending
from frameseek.knowledge_scheduler import KnowledgeScheduler
from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge
from frameseek.workspace import ident


def failure_state(code):
    return (
        "unknown"
        if "unknown" in code
        or "receipts_plan_conflict" in code
        or code.startswith("local_model_worker_stopped")
        else "failed"
    )


def select_local_resume_processor(processor, video, generation, metadata, digest, cache_key):
    """Use an older prompt only when its full source/model identity matches."""
    if not getattr(processor, "local_only", False) or not cache_key:
        return processor
    for config in (
        "knowledge-v3-8sec-8frames-4096tokens",
        "knowledge-v4-bound-8sec-8frames-4096tokens",
    ):
        legacy = VideoKnowledge(
            video=video,
            generation=generation,
            duration=metadata["duration"],
            source_fingerprint=digest,
            processor_revision=processor.revision,
            extraction_config=config,
            scene_boundaries=metadata.get("scene_boundaries", []),
        )
        if cache_key == legacy.cache_key():
            compatible = copy.copy(processor)
            compatible.extraction_config = config
            return compatible
    return processor


class CloudKnowledgeSpec:
    def __init__(self, agent, calibration, supplement_calibration=None):
        from frameseek.knowledge_budget import BASE_CONTRACT_CONFIG

        self.agent, self.calibration = agent, calibration
        self.supplement_calibration = supplement_calibration
        self.revision = agent.runtime.vision.revision
        self.extraction_config = BASE_CONTRACT_CONFIG.get(
            calibration.contract, "knowledge-v3-8sec-8frames-4096tokens"
        )

    def bind(self, source, owner, store, active):
        from frameseek.knowledge_tokenfactory import TokenFactoryKnowledgeProcessor

        return TokenFactoryKnowledgeProcessor(
            self.agent, source, owner, self.calibration, store=store, active=active
        )

    def bind_supplement(self, source, owner, store, question, plan, active):
        from frameseek.knowledge_tokenfactory import TokenFactorySupplementProcessor

        return TokenFactorySupplementProcessor(
            self.agent,
            source,
            owner,
            self.supplement_calibration,
            store=store,
            question=question,
            plan=plan,
            active=active,
        )


class FixtureKnowledgeJobs:
    def __init__(self, ws, app, settings):
        self.ws, self.app, self.settings = ws, app, settings
        self.scheduler = KnowledgeScheduler()
        self.decode_slots = threading.BoundedSemaphore(2)
        self.processor_lock = threading.Lock()
        self.local_processor = None
        self.dispatch_lock = threading.Lock()
        self.active_dispatch = None

    @contextmanager
    def call_scope(self, processor, active, *, interactive, token):
        with self.scheduler.slot(interactive=interactive, active=active):

            def acquired():
                with self.dispatch_lock:
                    self.active_dispatch = (token, True)

            try:
                context = (
                    processor.call_scope(active=active, on_acquired=acquired)
                    if getattr(processor, "local_only", False)
                    else getattr(processor, "call_scope", nullcontext)()
                )
                with context:
                    yield
            finally:
                with self.dispatch_lock:
                    if self.active_dispatch == (token, True):
                        self.active_dispatch = None

    def interrupt_dispatch(self, token):
        with self.dispatch_lock:
            if self.active_dispatch == (token, True):
                from frameseek.local_vision import cancel_worker

                cancel_worker()

    def processor(self):
        processor = getattr(self.app.state, "knowledge_fixture_processor", None)
        if self.settings.mode != "fixture":
            agent = self.app.state.agent
            if (
                os.environ.get("FS_KNOWLEDGE_TOKENFACTORY") == "1"
                and agent.runtime
                and agent.runtime.provider == "tokenfactory"
            ):
                from frameseek.knowledge_budget import ExtractionCalibration

                def load_calibration(key):
                    path = os.environ.get(key)
                    if not path:
                        return None
                    try:
                        with open(path, "rb") as stream:
                            raw = stream.read(16385)
                        if len(raw) > 16384:
                            return None
                        return ExtractionCalibration(**json.loads(raw))
                    except (OSError, TypeError, ValueError):
                        return None

                calibration = load_calibration("FS_KNOWLEDGE_CALIBRATION_FILE")
                if not calibration:
                    return None
                return CloudKnowledgeSpec(
                    agent, calibration, load_calibration("FS_KNOWLEDGE_SUPPLEMENT_CALIBRATION_FILE")
                )
            return None
        if getattr(processor, "fixture_only", False):
            return processor
        if os.environ.get("FS_KNOWLEDGE_LOCAL") != "1":
            return None
        from frameseek.knowledge_local import LocalKnowledgeProcessor, model_signature
        from frameseek.local_vision import configured

        configuration = configured()
        if not configuration:
            return None
        with self.processor_lock:
            if (
                not self.local_processor
                or self.local_processor.configuration != configuration
                or self.local_processor.signature != model_signature(configuration[1])
            ):
                self.local_processor = LocalKnowledgeProcessor(
                    configuration, self.ws.root / "knowledge-local" / "worker.log"
                )
            return self.local_processor

    def start(self, video, owner):
        source = self.ws.owned("videos", video, owner)
        metadata = json.loads(source["metadata"])
        if not metadata.get("duration"):
            raise HTTPException(409, "knowledge_probe_required")
        processor = self.processor()
        if processor is None:
            raise HTTPException(409, "knowledge_processor_unavailable")
        run_id, generation = ident("krun_"), source["generation"]
        total = math.ceil(metadata["duration"] / 8)
        # Resume older local checkpoints with their original prompt and cache key.
        # A different model or any other mismatch still fails closed in _work.
        if getattr(processor, "local_only", False):
            # Do not change the shared processor or supplement receipt identity.
            processor = copy.copy(processor)
            processor.extraction_config = "knowledge-v5-objects-8sec-8frames-4096tokens"
            with self.ws.db() as c:
                prior = c.execute(
                    "SELECT cache_key FROM knowledge_jobs WHERE video=? AND generation=?",
                    (video, generation),
                ).fetchone()
            if prior and prior["cache_key"]:
                processor = select_local_resume_processor(
                    processor,
                    video,
                    generation,
                    metadata,
                    source_digest(self.ws.root / video),
                    prior["cache_key"],
                )
        with self.ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM knowledge_jobs WHERE video=? AND generation=?",
                (video, generation),
            ).fetchone()
            if row and row["state"] in {"queued", "indexing", "ready"}:
                return dict(row)
            if row and row["state"] == "unknown":
                raise HTTPException(409, "knowledge_dispatch_unknown")
            c.execute(
                "INSERT INTO knowledge_jobs(video,generation,state,completed,total,updated,run_id) "
                "VALUES(?,?,'queued',0,?,?,?) ON CONFLICT(video,generation) DO UPDATE SET "
                "state='queued',total=excluded.total,run_id=excluded.run_id,error=NULL,"
                "updated=excluded.updated",
                (video, generation, total, time.time(), run_id),
            )
        try:
            threading.Thread(
                target=self._work, args=(source, owner, metadata, processor, run_id), daemon=True
            ).start()
        except Exception:
            # No processor was dispatched. Do not overwrite cancellation or a newer run.
            with self.ws.db() as c:
                c.execute(
                    "UPDATE knowledge_jobs SET state='failed',error='worker_start_failed',"
                    "updated=? WHERE video=? AND generation=? AND state='queued' AND run_id=?",
                    (time.time(), video, generation, run_id),
                )
            raise HTTPException(503, "worker_start_failed") from None
        return {"state": "queued", "generation": generation}

    def cancel(self, video, owner):
        source = self.ws.owned("videos", video, owner)
        with self.ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT run_id FROM knowledge_jobs WHERE video=? AND generation=? "
                "AND state IN ('queued','indexing')",
                (video, source["generation"]),
            ).fetchone()
            changed = c.execute(
                "UPDATE knowledge_jobs SET state='cancelled',error='cancelled',updated=? "
                "WHERE video=? AND generation=? AND state IN ('queued','indexing')",
                (time.time(), video, source["generation"]),
            )
            if not changed.rowcount:
                raise HTTPException(409, "knowledge_job_not_running")
        self.interrupt_dispatch(("base", row["run_id"]))

    def start_supplement(self, source, desk, owner, body, question, plan):
        processor = self.processor()
        if processor is None or (
            isinstance(processor, CloudKnowledgeSpec) and not processor.supplement_calibration
        ):
            raise HTTPException(409, "knowledge_processor_unavailable")
        from frameseek.workspace import digest

        payload_hash = digest(body.model_dump_json())
        with self.ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.ws.prune_supplements(c, now)
            current = c.execute(
                "SELECT v.generation,i.state FROM sessions s "
                "JOIN investigations i ON i.owner=s.id "
                "JOIN videos v ON v.owner=s.id AND v.id=i.video "
                "WHERE s.id=? AND i.id=? AND v.id=? "
                "AND s.expires>? AND i.expires>? AND v.expires>? "
                "AND i.deleted=0 AND v.deleted=0",
                (owner, desk["id"], source["id"], now, now, now),
            ).fetchone()
            if (
                not current
                or current["generation"] != source["generation"]
                or json.loads(current["state"]).get("epoch", 0) != body.generation
            ):
                raise HTTPException(409, "stale_chat_generation")
            old = c.execute(
                "SELECT * FROM knowledge_supplement_jobs WHERE owner=? AND request_id=?",
                (owner, body.request_id),
            ).fetchone()
            if old:
                if old["payload_hash"] != payload_hash or old["investigation"] != desk["id"]:
                    raise HTTPException(409, "request_payload_conflict")
                return dict(old)
            if c.execute("SELECT count(*) FROM knowledge_supplement_jobs").fetchone()[0] >= 1000:
                raise HTTPException(429, "knowledge_supplement_capacity")
            c.execute(
                "INSERT INTO knowledge_supplement_jobs(owner,request_id,investigation,video,"
                "generation,desk_epoch,payload_hash,state,base_version,updated,input) "
                "VALUES(?,?,?,?,?,?,?,'running',?,?,?)",
                (
                    owner,
                    body.request_id,
                    desk["id"],
                    source["id"],
                    source["generation"],
                    body.generation,
                    payload_hash,
                    plan["knowledge_version"],
                    time.time(),
                    body.model_dump_json(),
                ),
            )
        try:
            threading.Thread(
                target=self._supplement_work,
                args=(source, desk, owner, body, question, plan, processor),
                daemon=True,
            ).start()
        except Exception:
            # Keep request idempotency and preserve any concurrent terminal state.
            with self.ws.db() as c:
                c.execute(
                    "UPDATE knowledge_supplement_jobs "
                    "SET state='failed',error='worker_start_failed',updated=? "
                    "WHERE owner=? AND request_id=? AND investigation=? AND video=? "
                    "AND generation=? AND payload_hash=? AND state='running'",
                    (
                        time.time(),
                        owner,
                        body.request_id,
                        desk["id"],
                        source["id"],
                        source["generation"],
                        payload_hash,
                    ),
                )
            raise HTTPException(503, "worker_start_failed") from None
        return {"state": "running", "request_id": body.request_id}

    def preview_cloud_supplement(self, source, owner, question, plan):
        """Read-only admission estimate; execution rechecks and reserves per call."""
        from frameseek.video_knowledge import load_published_state

        processor = self.processor()
        if not isinstance(processor, CloudKnowledgeSpec) or not processor.supplement_calibration:
            raise BudgetDenied("Knowledge supplement is not calibrated or configured")
        with self.ws.db() as c:
            job = c.execute(
                "SELECT cache_key,version FROM knowledge_jobs "
                "WHERE video=? AND generation=? AND state='ready'",
                (source["id"], source["generation"]),
            ).fetchone()
        if not job or job["version"] != plan["knowledge_version"]:
            raise ValueError("knowledge_version_changed")
        directory = self.ws.root / source["id"]
        state = load_published_state(
            directory,
            source["id"],
            source["generation"],
            job["cache_key"],
            job["version"],
            source_digest(directory),
        )
        store = KnowledgeStore(directory, state)
        return processor.bind_supplement(
            source, owner, store, question, plan, lambda: True
        ).estimate

    def supplement_status(self, desk, owner, request_id):
        self.ws.owned("investigations", desk, owner)
        with self.ws.db() as c:
            row = c.execute(
                "SELECT * FROM knowledge_supplement_jobs "
                "WHERE owner=? AND investigation=? AND request_id=?",
                (owner, desk, request_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "knowledge_supplement_not_found")
        return {"state": row["state"], "version": row["version"], "error": row["error"]}

    def cancel_supplement(self, desk, owner, request_id):
        self.ws.owned("investigations", desk, owner)
        with self.ws.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT state FROM knowledge_supplement_jobs "
                "WHERE owner=? AND investigation=? AND request_id=?",
                (owner, desk, request_id),
            ).fetchone()
            if not row:
                raise HTTPException(409, "knowledge_supplement_not_running")
            if row["state"] in {"done", "failed", "unknown", "waiting_budget", "cancelled"}:
                return row["state"]
            result = c.execute(
                "UPDATE knowledge_supplement_jobs SET state='cancelled',updated=? "
                "WHERE owner=? AND investigation=? AND request_id=? AND state='running'",
                (time.time(), owner, desk, request_id),
            )
        if not result.rowcount:
            raise HTTPException(409, "knowledge_supplement_not_running")
        self.interrupt_dispatch(("supplement", owner, request_id))
        return "cancelled"

    def _supplement_work(self, source, desk, owner, body, question, plan, processor):
        from frameseek.knowledge_supplement import compile_local_supplement
        from frameseek.video_knowledge import load_published_state

        video, generation = source["id"], source["generation"]

        def finish_inactive(state="cancelled", error="context_expired_or_changed"):
            with self.ws.db() as c:
                c.execute(
                    "UPDATE knowledge_supplement_jobs SET state=?,error=?,updated=? "
                    "WHERE owner=? AND request_id=? AND state='running'",
                    (state, error[:100], time.time(), owner, body.request_id),
                )

        def active():
            try:
                current = self.ws.owned("videos", video, owner)
                latest_desk = self.ws.owned("investigations", desk["id"], owner)
                with self.ws.db() as c:
                    row = c.execute(
                        "SELECT state FROM knowledge_supplement_jobs "
                        "WHERE owner=? AND request_id=?",
                        (owner, body.request_id),
                    ).fetchone()
                return bool(
                    row
                    and row["state"] == "running"
                    and current["generation"] == generation
                    and latest_desk["video"] == video
                    and json.loads(latest_desk["state"]).get("epoch", 0) == body.generation
                    and self.ws.owner_active(owner)
                )
            except HTTPException:
                return False

        with nullcontext():
            if not active():
                finish_inactive()
                return
            try:
                with self.ws.db() as c:
                    job = c.execute(
                        "SELECT cache_key,version FROM knowledge_jobs "
                        "WHERE video=? AND generation=? AND state='ready'",
                        (video, generation),
                    ).fetchone()
                if not job or job["version"] != plan["knowledge_version"]:
                    raise ValueError("knowledge_version_changed")
                directory = self.ws.root / video
                state = load_published_state(
                    directory,
                    video,
                    generation,
                    job["cache_key"],
                    job["version"],
                    source_digest(directory),
                )
                store = KnowledgeStore(directory, state)
                if isinstance(processor, CloudKnowledgeSpec):
                    processor = processor.bind_supplement(
                        source, owner, store, question, plan, active
                    )
                version = compile_local_supplement(
                    store,
                    question,
                    plan,
                    processor,
                    active,
                    call_scope=lambda: self.call_scope(
                        processor,
                        active,
                        interactive=True,
                        token=("supplement", owner, body.request_id),
                    ),
                )
                if not active():
                    finish_inactive()
                    return
                with self.ws.db() as c:
                    c.execute("BEGIN IMMEDIATE")
                    current = c.execute(
                        "SELECT state FROM knowledge_supplement_jobs "
                        "WHERE owner=? AND request_id=?",
                        (owner, body.request_id),
                    ).fetchone()
                    if not current or current["state"] != "running":
                        return
                    desk_row = c.execute(
                        "SELECT video,state,deleted,expires FROM investigations "
                        "WHERE id=? AND owner=?",
                        (desk["id"], owner),
                    ).fetchone()
                    session = c.execute(
                        "SELECT expires FROM sessions WHERE id=?", (owner,)
                    ).fetchone()
                    if (
                        not desk_row
                        or desk_row["deleted"]
                        or desk_row["expires"] <= time.time()
                        or desk_row["video"] != video
                        or json.loads(desk_row["state"]).get("epoch", 0) != body.generation
                        or not session
                        or session["expires"] <= time.time()
                    ):
                        c.execute(
                            "UPDATE knowledge_supplement_jobs SET state='cancelled',updated=? "
                            "WHERE owner=? AND request_id=? AND state='running'",
                            (time.time(), owner, body.request_id),
                        )
                        return
                    changed = c.execute(
                        "UPDATE knowledge_jobs SET version=?,updated=? "
                        "WHERE video=? AND generation=? AND state='ready' AND version=? "
                        "AND EXISTS (SELECT 1 FROM videos WHERE id=? AND generation=? "
                        "AND deleted=0 AND expires>?)",
                        (
                            version,
                            time.time(),
                            video,
                            generation,
                            job["version"],
                            video,
                            generation,
                            time.time(),
                        ),
                    )
                    if not changed.rowcount:
                        raise ValueError("knowledge_version_changed")
                    c.execute(
                        "UPDATE knowledge_supplement_jobs SET state='done',version=?,updated=? "
                        "WHERE owner=? AND request_id=? AND state='running'",
                        (version, time.time(), owner, body.request_id),
                    )
            except Exception as error:
                code = str(error)
                state = (
                    "waiting_budget"
                    if isinstance(error, BudgetDenied) and "unknown" not in code.lower()
                    else failure_state(code.lower())
                )
                if active():
                    with self.ws.db() as c:
                        c.execute(
                            "UPDATE knowledge_supplement_jobs SET state=?,error=?,updated=? "
                            "WHERE owner=? AND request_id=? AND state='running'",
                            (state, code[:100], time.time(), owner, body.request_id),
                        )
                else:
                    finish_inactive("unknown" if state == "unknown" else "cancelled", code)

    def _work(self, source, owner, metadata, processor, run_id):
        video, generation = source["id"], source["generation"]

        def finish_inactive(state="cancelled", error="context_expired_or_changed"):
            with self.ws.db() as c:
                c.execute(
                    "UPDATE knowledge_jobs SET state=?,error=?,updated=? "
                    "WHERE video=? AND generation=? AND state='indexing' AND run_id=?",
                    (state, error[:100], time.time(), video, generation, run_id),
                )

        def active():
            try:
                current = self.ws.owned("videos", video, owner)
                with self.ws.db() as c:
                    row = c.execute(
                        "SELECT state,run_id FROM knowledge_jobs WHERE video=? AND generation=?",
                        (video, generation),
                    ).fetchone()
                return bool(
                    current["generation"] == generation
                    and row
                    and row["run_id"] == run_id
                    and row["state"] == "indexing"
                    and self.ws.owner_active(owner)
                )
            except HTTPException:
                return False

        with nullcontext():
            with self.ws.db() as c:
                claimed = c.execute(
                    "UPDATE knowledge_jobs SET state='indexing',updated=? "
                    "WHERE video=? AND generation=? AND state='queued' AND run_id=?",
                    (time.time(), video, generation, run_id),
                )
            if not claimed.rowcount:
                return
            if not active():
                finish_inactive()
                return
            try:
                directory = self.ws.root / video
                identity = VideoKnowledge(
                    video=video,
                    generation=generation,
                    duration=metadata["duration"],
                    source_fingerprint=source_digest(directory),
                    processor_revision=processor.revision,
                    extraction_config=processor.extraction_config,
                    scene_boundaries=metadata.get("scene_boundaries", []),
                )
                with self.ws.db() as c:
                    current = c.execute(
                        "SELECT cache_key FROM knowledge_jobs WHERE video=? AND generation=? "
                        "AND state='indexing' AND run_id=?",
                        (video, generation, run_id),
                    ).fetchone()
                if (
                    current
                    and current["cache_key"]
                    and current["cache_key"] != identity.cache_key()
                ):
                    # Do not silently restart completed work under a different
                    # processor or model configuration on an ordinary resume.
                    raise ValueError("knowledge_resume_processor_changed")
                store = KnowledgeStore(directory, identity)
                if isinstance(processor, CloudKnowledgeSpec):
                    processor = processor.bind(source, owner, store, active)

                def progress(_ordinal, pending):
                    with self.ws.db() as c:
                        c.execute(
                            "UPDATE knowledge_jobs SET completed=total-?,cache_key=?,updated=? "
                            "WHERE video=? AND generation=? AND state='indexing' AND run_id=?",
                            (pending, store.key, time.time(), video, generation, run_id),
                        )

                from frameseek.media import frames

                def bounded_decode(*args):
                    with self.decode_slots:
                        if not active():
                            raise ValueError("knowledge_cancelled")
                        return frames(*args)

                version = compile_pending(
                    store,
                    metadata,
                    processor.infer,
                    active,
                    progress,
                    decode=bounded_decode,
                    call_scope=lambda: self.call_scope(
                        processor, active, interactive=False, token=("base", run_id)
                    ),
                )
                if active():
                    with self.ws.db() as c:
                        published = c.execute(
                            "UPDATE knowledge_jobs SET state='ready',completed=total,"
                            "version=?,cache_key=?,updated=? WHERE video=? AND generation=? "
                            "AND state='indexing' AND run_id=? AND EXISTS "
                            "(SELECT 1 FROM videos WHERE id=? AND generation=? AND deleted=0 "
                            "AND expires>?) AND EXISTS "
                            "(SELECT 1 FROM sessions WHERE id=? AND expires>?)",
                            (
                                version,
                                store.key,
                                time.time(),
                                video,
                                generation,
                                run_id,
                                video,
                                generation,
                                time.time(),
                                owner,
                                time.time(),
                            ),
                        )
                    if not published.rowcount:
                        finish_inactive()
                else:
                    finish_inactive()
            except Exception as error:
                code = str(error)
                state = (
                    "waiting_budget"
                    if isinstance(error, BudgetDenied) and "unknown" not in code.lower()
                    else failure_state(code.lower())
                )
                if active():
                    with self.ws.db() as c:
                        c.execute(
                            "UPDATE knowledge_jobs SET state=?,error=?,updated=? "
                            "WHERE video=? AND generation=? AND state='indexing' AND run_id=?",
                            (state, code[:100], time.time(), video, generation, run_id),
                        )
                else:
                    finish_inactive("unknown" if state == "unknown" else "cancelled", code)
