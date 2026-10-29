"""Mock-only Token Factory adapter checks; no network or live approvals."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_agent import approve
from test_knowledge_budget import fixtures
from test_tokenfactory import configuration

from frameseek.agent import AgentError, Provider
from frameseek.attempts import AttemptLedger, BudgetDenied
from frameseek.index_service import source_digest
from frameseek.knowledge_budget import ExtractionCalibration, admit_base_index_store
from frameseek.knowledge_compiler import extraction_schema
from frameseek.knowledge_tokenfactory import (
    TokenFactoryKnowledgeProcessor,
    TokenFactorySupplementProcessor,
)
from frameseek.video_knowledge import KnowledgeStore, VideoKnowledge


def source_store(directory, runtime):
    return KnowledgeStore(
        directory,
        VideoKnowledge(
            video="vid_fixture",
            generation=1,
            duration=8,
            source_fingerprint=source_digest(directory),
            processor_revision=runtime.vision.revision,
            extraction_config="knowledge-v3-8sec-8frames-4096tokens",
        ),
    )


def test_adapter_requires_preflight_and_routes_to_coarse_ingest(tmp_path):
    ledger, runtime, calibration = fixtures(tmp_path)
    runtime.vision.model = "synthetic"
    runtime.attempt_seconds = 10
    directory = tmp_path / "workspace" / "vid_fixture"
    (directory / "frames").mkdir(parents=True)
    (directory / "original").write_bytes(b"synthetic source")
    frame = directory / "frames" / "f.jpg"
    frame.write_bytes(b"\xff\xd8\xffsynthetic\xff\xd9")
    calls = []

    class Provider:
        def __init__(self):
            self.runtime = runtime
            self.check = lambda: None

        def call(self, role, route, payload, **kwargs):
            self.check()
            calls.append((role, route, payload, kwargs))
            content = {"frames": [{"index": "0", "description": "fixture", "subjects": []}]}
            return {"choices": [{"message": {"content": json.dumps(content)}}]}

    provider = Provider()
    agent = SimpleNamespace(
        ledger=ledger,
        runtime=runtime,
        ws=SimpleNamespace(root=tmp_path / "workspace"),
        provider=lambda *_: provider,
    )
    source = {"id": "vid_fixture", "generation": 1}
    store = source_store(directory, runtime)
    with pytest.raises(BudgetDenied):
        TokenFactoryKnowledgeProcessor(
            agent, source, "owner", None, store=store, active=lambda: True
        )
    assert not calls
    processor = TokenFactoryKnowledgeProcessor(
        agent,
        source,
        "owner",
        calibration,
        store=store,
        active=lambda: True,
    )
    assert processor.estimate["new_calls"] == 1 and not calls
    processor.infer("fixture", [frame], schema=extraction_schema(1))
    assert len(calls) == 1
    _, _, payload, kwargs = calls[0]
    assert payload["max_tokens"] == 4096
    assert kwargs["kind"] == "coarse" and kwargs["category"] == "ingest"
    assert kwargs["owner"] == "owner" and kwargs["version"] == "fixture"
    with pytest.raises(AgentError):
        processor.infer("fixture", [frame], schema={})


def test_mock_transport_reserves_both_ledger_scopes_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TF_KEY", "synthetic-only")
    runtime = configuration()
    ledger = AttemptLedger(tmp_path / "ledger.sqlite3")
    approve(ledger, "owner", calls=4, money=4)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        content = {"frames": [{"index": "0", "description": "fixture", "subjects": []}]}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]
            },
        )

    provider = Provider(runtime, ledger, lambda: None, httpx.MockTransport(respond))
    source_dir = tmp_path / "workspace" / "vid_fixture"
    (source_dir / "frames").mkdir(parents=True)
    (source_dir / "original").write_bytes(b"synthetic source")
    frame = source_dir / "frames" / "f.jpg"
    frame.write_bytes(b"\xff\xd8\xffsynthetic\xff\xd9")
    calibration = ExtractionCalibration(
        reference="synthetic test only",
        processor_revision=runtime.vision.revision,
        contract="video_knowledge_extraction_v3",
        max_frames=8,
        max_output_tokens=4096,
        max_cost_units=1,
        currency_unit="USD_MICRO",
        valid_until=time.time() + 120,
    )
    agent = SimpleNamespace(
        ledger=ledger,
        runtime=runtime,
        ws=SimpleNamespace(root=tmp_path / "workspace"),
        provider=lambda *_: provider,
    )
    processor = TokenFactoryKnowledgeProcessor(
        agent,
        {"id": "vid_fixture", "generation": 1},
        "owner",
        calibration,
        store=source_store(source_dir, runtime),
        active=lambda: True,
    )
    processor.infer("synthetic", [frame], schema=extraction_schema(1))
    assert len(calls) == 1
    with ledger._connect() as connection:
        attempt = connection.execute("SELECT kind,state FROM attempts").fetchone()
        budgets = connection.execute("SELECT used_calls,used_primary FROM budgets").fetchall()
    assert tuple(attempt) == ("coarse", "completed")
    assert sorted(tuple(row) for row in budgets) == [(1, 1), (1, 1)]


def test_preflight_uses_validated_receipts_and_blocks_unknown(tmp_path):
    from test_knowledge_compiler import decode, infer

    ledger, runtime, calibration = fixtures(tmp_path)
    directory = tmp_path / "vid_fixture"
    (directory / "frames").mkdir(parents=True)
    (directory / "original").write_bytes(b"synthetic source")
    store = source_store(directory, runtime)
    rows = decode(directory, "vid_fixture", {}, {"start": 0}, 8, "fixture")
    decoded = store.base / "window-0000.decoded.json"
    marker = store.base / "window-0000.dispatched.json"
    raw = store.base / "window-0000.raw.json"
    decoded.write_text(json.dumps(rows))
    marker.write_text("{}")
    with pytest.raises(BudgetDenied, match="outcome unknown"):
        admit_base_index_store(ledger, "owner", runtime, calibration, store)
    raw.write_text(json.dumps(infer("fixture", [None, None], None)))
    estimate = admit_base_index_store(ledger, "owner", runtime, calibration, store)
    assert estimate["new_calls"] == 0 and estimate["completed_windows"] == 1
    assert store.load().pending() == [0]  # preflight never writes checkpoints
    from frameseek.knowledge_compiler import packet_to_segment
    from frameseek.occurrences import fingerprint

    state = store.load()
    store.checkpoint(
        packet_to_segment(state, 0, rows, json.loads(raw.read_text())),
        fingerprint(state.model_dump(mode="json")),
    )
    (directory / "frames" / rows[0]["file"]).unlink()
    with pytest.raises(BudgetDenied, match="checkpoint evidence missing"):
        admit_base_index_store(ledger, "owner", runtime, calibration, store)
    (directory / "frames" / rows[0]["file"]).write_bytes(b"fixture image")
    (store.base / "checkpoint.json").unlink()
    raw.write_text(json.dumps({"text": "not-json"}))
    with pytest.raises(BudgetDenied, match="Invalid cached"):
        admit_base_index_store(ledger, "owner", runtime, calibration, store)
    with ledger._connect() as connection:
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_v4_requires_its_own_calibration_and_keeps_unknown_dispatch_blocked(tmp_path):
    ledger, runtime, v3_calibration = fixtures(tmp_path)
    directory = tmp_path / "vid_fixture"
    (directory / "frames").mkdir(parents=True)
    (directory / "original").write_bytes(b"synthetic source")
    v3 = source_store(directory, runtime)
    v4_state = v3.identity.model_copy(
        update={"extraction_config": "knowledge-v4-bound-8sec-8frames-4096tokens"}
    )
    v4 = KnowledgeStore(directory, v4_state)
    v4_calibration = replace(v3_calibration, contract="video_knowledge_extraction_v4")
    v5 = KnowledgeStore(
        directory,
        v3.identity.model_copy(
            update={"extraction_config": "knowledge-v5-objects-8sec-8frames-4096tokens"}
        ),
    )
    v5_calibration = replace(v3_calibration, contract="video_knowledge_extraction_v5")
    with pytest.raises(BudgetDenied, match="identity"):
        admit_base_index_store(ledger, "owner", runtime, v3_calibration, v4)
    with pytest.raises(BudgetDenied, match="identity"):
        admit_base_index_store(ledger, "owner", runtime, v4_calibration, v3)
    with pytest.raises(BudgetDenied, match="identity"):
        admit_base_index_store(ledger, "owner", runtime, v4_calibration, v5)
    assert admit_base_index_store(ledger, "owner", runtime, v5_calibration, v5)["new_calls"] == 1
    assert admit_base_index_store(ledger, "owner", runtime, v4_calibration, v4)["new_calls"] == 1
    (v4.base / "window-0000.dispatched.json").write_text("{}")
    with pytest.raises(BudgetDenied, match="outcome unknown"):
        admit_base_index_store(ledger, "owner", runtime, v4_calibration, v4)
    with ledger._connect() as connection:
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_mock_cloud_compiler_resumes_saved_reply_without_second_charge(tmp_path, monkeypatch):
    from test_knowledge_compiler import decode, infer

    from frameseek.knowledge_compiler import compile_pending

    monkeypatch.setenv("TEST_TF_KEY", "synthetic-only")
    runtime = configuration()
    ledger = AttemptLedger(tmp_path / "ledger.sqlite3")
    approve(ledger, "owner", calls=4, money=4)
    requests = []
    running = True

    def respond(request):
        payload = json.loads(request.content)
        image_count = sum(x.get("type") == "image_url" for x in payload["messages"][1]["content"])
        result = infer("fixture", [None] * image_count, None)
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": result["text"]}, "finish_reason": "stop"}]},
        )

    provider = Provider(runtime, ledger, lambda: None, httpx.MockTransport(respond))
    directory = tmp_path / "workspace" / "vid_fixture"
    (directory / "frames").mkdir(parents=True)
    (directory / "original").write_bytes(b"synthetic source")
    store = source_store(directory, runtime)
    agent = SimpleNamespace(
        ledger=ledger,
        runtime=runtime,
        ws=SimpleNamespace(root=tmp_path / "workspace"),
        provider=lambda *_: provider,
    )
    calibration = ExtractionCalibration(
        reference="synthetic test only",
        processor_revision=runtime.vision.revision,
        contract="video_knowledge_extraction_v3",
        max_frames=8,
        max_output_tokens=4096,
        max_cost_units=1,
        currency_unit="USD_MICRO",
        valid_until=time.time() + 120,
    )
    source = {"id": "vid_fixture", "generation": 1}
    processor = TokenFactoryKnowledgeProcessor(
        agent, source, "owner", calibration, store=store, active=lambda: running
    )

    def decode_jpegs(*args):
        rows = decode(*args)
        for row in rows:
            (directory / "frames" / row["file"]).write_bytes(b"\xff\xd8\xfffixture\xff\xd9")
        return rows

    def stop_after_reply(*args, **kwargs):
        nonlocal running
        reply = processor.infer(*args, **kwargs)
        running = False
        return reply

    with pytest.raises(ValueError, match="knowledge_cancelled"):
        compile_pending(
            store, {"duration": 8}, stop_after_reply, lambda: running, decode=decode_jpegs
        )
    assert len(requests) == 1 and store.load().pending() == [0]
    running = True
    processor = TokenFactoryKnowledgeProcessor(
        agent, source, "owner", calibration, store=store, active=lambda: running
    )
    assert processor.estimate["new_calls"] == 0
    version = compile_pending(
        store, {"duration": 8}, processor.infer, lambda: running, decode=decode_jpegs
    )
    assert not store.read_version(version).pending() and len(requests) == 1
    with ledger._connect() as connection:
        budgets = connection.execute("SELECT used_calls FROM budgets").fetchall()
    assert sorted(row[0] for row in budgets) == [1, 1]


def test_mock_cloud_supplement_is_bounded_and_reuses_cached_evidence(tmp_path, monkeypatch):
    from test_knowledge_supplement import FixtureSupplement, ready

    from frameseek.knowledge_supplement import compile_local_supplement

    monkeypatch.setenv("TEST_TF_KEY", "synthetic-only")
    runtime = configuration()
    ledger = AttemptLedger(tmp_path / "ledger.sqlite3")
    approve(ledger, "owner", calls=20, money=20)
    directory = tmp_path / "vid_fixture"
    directory.mkdir()
    store, original, question, plan = ready(directory)
    for number, frame in enumerate(store.load().publishable_index().frames):
        (directory / "frames" / frame.file).write_bytes(
            b"\xff\xd8\xfffixture" + bytes([number]) + b"\xff\xd9"
        )
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        text = payload["messages"][1]["content"][0]["text"]
        schema = {"properties": {"visible": {}}} if "Choose yes only" in text else {}
        reply = FixtureSupplement().infer("fixture", [None], schema)
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": reply["text"]}, "finish_reason": "stop"}]},
        )

    provider = Provider(runtime, ledger, lambda: None, httpx.MockTransport(respond))
    agent = SimpleNamespace(
        ledger=ledger,
        runtime=runtime,
        ws=SimpleNamespace(root=tmp_path),
        provider=lambda *_: provider,
    )
    calibration = ExtractionCalibration(
        reference="synthetic test only",
        processor_revision=runtime.vision.revision,
        contract="video_knowledge_supplement_v6",
        max_frames=1,
        max_output_tokens=4096,
        max_cost_units=1,
        currency_unit="USD_MICRO",
        valid_until=time.time() + 120,
    )
    source = {"id": "vid_fixture", "generation": 1}
    processor = TokenFactorySupplementProcessor(
        agent,
        source,
        "owner",
        calibration,
        store=store,
        question=question,
        plan=plan,
        active=lambda: True,
    )
    assert processor.estimate["new_calls"] == 8
    version = compile_local_supplement(store, question, plan, processor, lambda: True)
    assert version != original and len(calls) == 8
    assert compile_local_supplement(store, question, plan, processor, lambda: True) == version
    assert len(calls) == 8
    with ledger._connect() as connection:
        assert sorted(row[0] for row in connection.execute("SELECT used_calls FROM budgets")) == [
            8,
            8,
        ]


def test_cloud_supplement_denies_inadequate_budget_or_unknown_receipt(tmp_path, monkeypatch):
    from test_knowledge_supplement import FixtureSupplement, ready

    from frameseek.knowledge_supplement import supplement_request_id

    monkeypatch.setenv("TEST_TF_KEY", "synthetic-only")
    runtime = configuration()
    ledger = AttemptLedger(tmp_path / "ledger.sqlite3")
    approve(ledger, "owner", calls=1, money=1)
    directory = tmp_path / "vid_fixture"
    directory.mkdir()
    store, _, question, plan = ready(directory)
    agent = SimpleNamespace(ledger=ledger, runtime=runtime, ws=SimpleNamespace(root=tmp_path))
    calibration = ExtractionCalibration(
        reference="synthetic only",
        processor_revision=runtime.vision.revision,
        contract="video_knowledge_supplement_v6",
        max_frames=1,
        max_output_tokens=4096,
        max_cost_units=1,
        currency_unit="USD_MICRO",
        valid_until=time.time() + 120,
    )
    source = {"id": "vid_fixture", "generation": 1}

    def build():
        return TokenFactorySupplementProcessor(
            agent,
            source,
            "owner",
            calibration,
            store=store,
            question=question,
            plan=plan,
            active=lambda: True,
        )

    with pytest.raises(BudgetDenied):
        build()
    processor = SimpleNamespace(
        revision=runtime.vision.revision, extraction_config="knowledge-v3-8sec-8frames-4096tokens"
    )
    request_id = supplement_request_id(store.load(), question, processor)
    window = plan["windows"][0]
    marker = store.base / (
        f"supplement-{request_id}-{plan['plan_id']}-{window['ordinal']:04d}-00-extract.dispatched.json"
    )
    marker.write_text("{}")
    with pytest.raises(BudgetDenied, match="outcome unknown"):
        build()
    marker.unlink()
    other_plan = store.base / f"supplement-{request_id}-other-plan-0000-00-extract.dispatched.json"
    other_plan.write_text("{}")
    with pytest.raises(BudgetDenied, match="outcome unknown"):
        build()
    raw = other_plan.with_name(other_plan.name.replace(".dispatched.json", ".raw.json"))
    raw.write_text(json.dumps(FixtureSupplement().infer("fixture", [None], {"type": "object"})))
    with pytest.raises(BudgetDenied, match="mapping unknown"):
        build()
    raw.unlink()
    other_plan.unlink()
    old_id = supplement_request_id(store.load(), question, processor, "single_frame_verified_v5")
    legacy = store.base / f"supplement-{old_id}-old-plan-0000-00-extract.dispatched.json"
    legacy.write_text("{}")
    with pytest.raises(BudgetDenied, match="outcome unknown"):
        build()
    legacy.unlink()
    from dataclasses import replace

    calibration = replace(calibration, contract="video_knowledge_supplement_v5")
    with pytest.raises(BudgetDenied, match="calibration"):
        build()
    with ledger._connect() as connection:
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
