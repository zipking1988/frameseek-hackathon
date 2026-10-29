"""Gemini transport contracts with actual local decoding; no live calls or cloud resources."""

import json
import time
from pathlib import Path

import httpx
import pytest
from test_agent import approve, setup_real

from frameseek.agent import AgentError, Endpoint, Provider, Runtime, load_runtime
from frameseek.attempts import AttemptLedger, BudgetDenied
from frameseek.config import Settings
from frameseek.gemini import BASE_URL, normalize_response, prepare_request
from frameseek.readiness import readiness_report
from frameseek.schema import Answer


@pytest.fixture
def anyio_backend():
    return "asyncio"


def configuration():
    def endpoint(model):
        return Endpoint(
            base_url=BASE_URL,
            model=model,
            revision="MOCK-NOT-LIVE",
            key_env="TEST_GEMINI_KEY",
            max_cost_micro=1,
        )

    return Runtime(
        provider="gemini",
        allow_google_media=True,
        vision=endpoint("gemini-test"),
        text=endpoint("gemini-test"),
        rerank=endpoint("gemini-test"),
        embed=endpoint("gemini-embedding-test"),
        budget_version="test_v1",
        calibration_reference="MOCK ONLY",
        resource_cutoff_reference="MOCK ONLY",
        valid_until=time.time() + 3600,
        attempt_seconds=2,
    )


def response(result, finish="STOP"):
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "finishReason": finish,
                    "content": {
                        "parts": [
                            {"text": "untrusted internal thought", "thought": True},
                            {"text": json.dumps(result, ensure_ascii=False)},
                        ]
                    },
                }
            ]
        },
    )


def handler(calls, *, fake_frame=False):
    def respond(request):
        payload = json.loads(request.content)
        calls.append((request, payload))
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "test-secret"
        assert "authorization" not in request.headers and "key" not in request.url.params
        if request.url.path.endswith(":batchEmbedContents"):
            return httpx.Response(
                200, json={"embeddings": [{"values": [1.0, 0.0]} for _ in payload["requests"]]}
            )
        assert request.url.path.endswith(":generateContent")
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        parts = payload["contents"][0]["parts"]
        prompt = parts[0]["text"]
        if any("inlineData" in p for p in parts):
            ids = [json.loads(p["text"])["frame_id"] for p in parts[1:] if "text" in p]
            if prompt.startswith("Describe"):
                return response(
                    {"frames": [{"frame_id": f, "caption": "纸箱在工作台上"} for f in ids]}
                )
            return response(
                {
                    "observations": [
                        {
                            "frame_ids": ["invented-frame" if fake_frame else ids[0]],
                            "statement": "纸箱出现在工作台上。",
                        }
                    ],
                    "unresolved": [],
                }
            )
        prompt = json.loads(prompt)
        if "documents" in prompt:
            return response({"results": [{"index": i} for i in range(len(prompt["documents"]))]})
        if "candidates" in prompt:
            return response({"inspect": list(range(min(3, len(prompt["candidates"]))))})
        return response(
            {"answer": "可以在引用画面中看到纸箱。", "followups": ["之后呢？"], "unresolved": []}
        )

    return respond


@pytest.mark.anyio
async def test_gemini_upload_index_followup_evidence_ownership_and_idempotency(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_GEMINI_KEY", "test-secret")
    calls = []
    app, client, video, desk = await setup_real(
        tmp_path,
        monkeypatch,
        calls,
        runtime_factory=configuration,
        response_handler=handler(calls),
    )
    try:
        assert (await client.get("/api/session")).json()["provider"] == "gemini"
        status = (await client.get("/api/runtime/status")).json()
        assert status["provider"] == "gemini" and not status["live_verified"]
        assert "test-secret" not in json.dumps(status)
        body = {
            "investigation_id": desk["id"],
            "request_id": "req_gemini_first",
            "generation": 0,
            "question": "纸箱在哪？",
        }
        result = await client.post("/api/chat", json=body)
        assert result.status_code == 200, result.text
        answer = Answer.model_validate(result.json()["response"])
        assert answer.events and answer.schema_version == "0.1"
        assert "thought" not in answer.answer
        for event in answer.events:
            assert event.video_id == video
            for frame in event.evidence:
                assert (await client.get(frame.uri)).status_code == 200
                assert 0 <= frame.t <= 18
        before = len(calls)
        assert (await client.post("/api/chat", json=body)).json() == result.json()
        assert len(calls) == before
        follow = await client.post(
            "/api/chat",
            json={
                **body,
                "request_id": "req_gemini_follow",
                "question": "它后来呢？",
                "parent_request_id": body["request_id"],
            },
        )
        assert follow.status_code == 200 and follow.json()["usedContext"]
        assert any("Selected prior question" in json.dumps(p) for _, p in calls)
        with app.state.agent.ledger._connect() as conn:
            rows = conn.execute("SELECT kind,state FROM attempts").fetchall()
        assert len(rows) == len(calls) and all(r["state"] == "completed" for r in rows)
        assert sum(r["kind"] == "zoom" for r in rows) <= 6
        for req, payload in calls:
            if ":batchEmbedContents" in req.url.path:
                assert "inlineData" not in json.dumps(payload)
                assert all(
                    r["embedContentConfig"]["taskType"] in {"RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"}
                    for r in payload["requests"]
                )
                assert all(
                    r["embedContentConfig"]["autoTruncate"] is False for r in payload["requests"]
                )
            elif payload["contents"][0]["parts"][0]["text"].startswith("{"):
                # Only visual verification has inline JPEGs; planning/rerank/synthesis do not.
                if "inlineData" in json.dumps(payload):
                    assert "observations" in payload["contents"][0]["parts"][0]["text"]
        foreign = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        async with foreign:
            await foreign.get("/api/session")
            assert (await foreign.get(answer.events[0].evidence[0].uri)).status_code == 404
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_gemini_cannot_accept_invented_frame_references(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GEMINI_KEY", "test-secret")
    calls = []
    _, client, _, desk = await setup_real(
        tmp_path,
        monkeypatch,
        calls,
        runtime_factory=configuration,
        response_handler=handler(calls, fake_frame=True),
    )
    try:
        result = await client.post(
            "/api/chat",
            json={
                "investigation_id": desk["id"],
                "request_id": "req_fake_frame",
                "generation": 0,
                "question": "纸箱在哪？",
            },
        )
        assert result.status_code == 502, result.text
        assert result.json()["response"]["status"] == "error"
        assert not result.json()["response"]["events"]
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("host", "gemini_endpoint_not_allowed"),
        ("http", "gemini_endpoint_not_allowed"),
        ("model", "invalid_gemini_model"),
        ("consent", "google_media_not_enabled"),
        ("asr", "gemini_asr_not_supported"),
        ("key", "model_key_missing"),
    ],
)
def test_gemini_configuration_blocks_unsafe_routes(monkeypatch, mutation, code):
    monkeypatch.setenv("TEST_GEMINI_KEY", "test-secret")
    config = configuration()
    assert not config.blockers()
    if mutation == "host":
        config.vision.base_url = "https://other.example/v1beta"
    if mutation == "http":
        config.text.base_url = "http://generativelanguage.googleapis.com/v1beta"
    if mutation == "model":
        config.embed.model = "bad/model?key=secret"
    if mutation == "consent":
        config.allow_google_media = False
    if mutation == "asr":
        config.asr = config.vision
    if mutation == "key":
        config.text.key_env = None
    assert code in config.blockers()
    assert not readiness_report(Settings(mode="real"), config, approved=True)[
        "dispatch_prerequisites_met"
    ]


def provider(tmp_path, monkeypatch, respond):
    monkeypatch.setenv("TEST_GEMINI_KEY", "test-secret")
    ledger = AttemptLedger(tmp_path / "attempts.sqlite3")
    config = configuration()
    return Provider(config, ledger, lambda: None, httpx.MockTransport(respond))


def kwargs():
    return {
        "owner": "owner",
        "attempt": "req_test_plan",
        "kind": "text",
        "version": "test_v1",
        "category": "query",
        "deadline": time.monotonic() + 30,
    }


def test_gemini_unapproved_budget_sends_nothing(tmp_path, monkeypatch):
    calls = []
    p = provider(tmp_path, monkeypatch, lambda req: calls.append(req))
    with pytest.raises(BudgetDenied):
        p.chat("text", "{}", **kwargs())
    assert not calls


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION"])
def test_gemini_incomplete_results_are_unknown_and_never_resent(tmp_path, monkeypatch, finish):
    calls = []

    def respond(req):
        calls.append(req)
        return response({"inspect": [0]}, finish)

    p = provider(tmp_path, monkeypatch, respond)
    approve(p.ledger, "owner")
    with pytest.raises(AgentError, match="model_unknown"):
        p.chat("text", "{}", **kwargs())
    assert p.ledger.read_attempt("req_test_plan", "owner")["state"] == "unknown"
    with pytest.raises(AgentError, match="unknown_attempt_do_not_resend"):
        p.chat("text", "{}", **kwargs())
    assert len(calls) == 1


def test_gemini_external_media_and_oversized_requests_rejected_before_reservation(
    tmp_path, monkeypatch
):
    calls = []
    p = provider(tmp_path, monkeypatch, lambda req: calls.append(req))
    approve(p.ledger, "owner")
    with pytest.raises(AgentError, match="gemini_inline_jpeg_required"):
        p.chat(
            "vision",
            [{"type": "image_url", "image_url": {"url": "https://private.example/x.jpg"}}],
            **kwargs(),
        )
    monkeypatch.setattr("frameseek.gemini.MAX_REQUEST_BYTES", 200)
    with pytest.raises(AgentError, match="gemini_request_limit"):
        p.chat("text", "too large", **kwargs())
    assert not calls and p.ledger.read_attempt("req_test_plan", "owner") is None


def test_gemini_ignores_thoughts_and_rejects_function_calls():
    content = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"thought": True, "text": '{"wrong":true}'},
                        {"text": '{"inspect":'},
                        {"text": "[0]}"},
                    ]
                },
            }
        ]
    }
    assert (
        normalize_response("text", content)["choices"][0]["message"]["content"] == '{"inspect":[0]}'
    )
    content["candidates"][0]["content"]["parts"] = [{"functionCall": {"name": "tool"}}]
    with pytest.raises(AgentError):
        normalize_response("text", content)


def test_templates_are_valid_but_blocked_and_legacy_route_stays_default(monkeypatch):
    for name, expected in [
        ("runtime.example.json", "nvidia_nebius"),
        ("runtime.gemini.example.json", "gemini"),
    ]:
        monkeypatch.setenv("FS_RUNTIME_FILE", str(Path("docs") / name))
        config = load_runtime()
        assert config.provider == expected
        assert "calibration_expired" in config.blockers()
        assert "resource_cost_bound_unverified" in config.blockers()
    _, wire = prepare_request(
        "embed", "/embeddings", {"model": "test", "input": ["one", "two"]}, "ingest"
    )
    assert len(wire["requests"]) == 2
