"""No network/spend: Token Factory contracts use isolated ledgers and mock transports."""

import base64
import json
import logging
import time
from pathlib import Path

import httpx
import pytest
from test_agent import approve, setup_real
from test_agent import handler as legacy_handler

from frameseek.agent import AgentError, Endpoint, Provider, Runtime, frozen_configuration_matches
from frameseek.attempts import AttemptLedger, BudgetDenied
from frameseek.config import Settings
from frameseek.readiness import readiness_report
from frameseek.tokenfactory import BASE_URL, EMBED_MODEL, VISION_MODEL, prepare_request


@pytest.fixture
def anyio_backend():
    return "asyncio"


def configuration():
    def endpoint(model):
        return Endpoint(
            base_url=BASE_URL,
            model=model,
            revision="MOCK-ONLY",
            key_env="TEST_TF_KEY",
            max_cost_micro=1,
        )

    return Runtime(
        provider="tokenfactory",
        allow_tokenfactory_media=True,
        vision=endpoint(VISION_MODEL),
        text=endpoint(VISION_MODEL),
        rerank=endpoint(VISION_MODEL),
        embed=endpoint(EMBED_MODEL),
        budget_version="test_v1",
        calibration_reference="MOCK-ONLY",
        resource_cutoff_reference="MOCK-ONLY",
        valid_until=time.time() + 3600,
        attempt_seconds=2,
    )


def kwargs():
    return dict(
        owner="owner",
        attempt="req_tf",
        kind="text",
        version="test_v1",
        category="query",
        deadline=time.monotonic() + 30,
    )


def provider(tmp_path, monkeypatch, handler):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    return Provider(
        configuration(),
        AttemptLedger(tmp_path / "attempts.sqlite3"),
        lambda: None,
        httpx.MockTransport(handler),
    )


def handler(calls):
    fallback = legacy_handler([])

    def respond(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        assert str(request.url).startswith(BASE_URL + "/")
        assert request.headers["authorization"] == "Bearer mock-secret"
        if request.url.path.endswith("/embeddings"):
            assert payload["model"] == EMBED_MODEL
            assert all(isinstance(t, str) for t in payload["input"])
            return fallback(request)
        assert request.url.path.endswith("/chat/completions")
        assert payload["model"] == VISION_MODEL
        assert payload["max_completion_tokens"] == 4096
        assert payload["response_format"] == {"type": "json_object"}
        content = payload["messages"][1]["content"]
        assert payload["service_tier"] == "default"
        if isinstance(content, list):
            assert sum(item.get("type") == "image_url" for item in content) <= 8
        if isinstance(content, str) and "documents" in json.loads(content):
            docs = json.loads(content)["documents"]
            result = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"results": [{"index": i} for i in range(min(3, len(docs)))]}
                            )
                        }
                    }
                ]
            }
        else:
            result = fallback(request).json()
        result["choices"][0]["finish_reason"] = "stop"
        result["choices"][0]["message"]["reasoning_content"] = "DO NOT DISPLAY"
        return httpx.Response(200, json=result)

    return respond


@pytest.mark.anyio
async def test_index_chat_followup_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    app, client, video, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=handler(calls)
    )
    try:
        status = (await client.get("/api/runtime/status")).json()
        assert status["provider"] == "tokenfactory"
        assert not status["live_verified"]
        body = dict(
            investigation_id=desk["id"],
            request_id="req_tf_first",
            generation=0,
            question="纸箱在哪？",
        )
        result = await client.post("/api/chat", json=body)
        assert result.status_code == 200, result.text
        assert "DO NOT DISPLAY" not in result.text
        answer = result.json()["response"]
        assert answer["events"]
        for event in answer["events"]:
            for frame in event["evidence"]:
                assert (await client.get(frame["uri"])).status_code == 200
                assert 0 <= frame["t"] <= 18
        before = len(calls)
        assert (await client.post("/api/chat", json=body)).json() == result.json()
        assert len(calls) == before
        follow = await client.post(
            "/api/chat",
            json={
                **body,
                "request_id": "req_tf_follow",
                "parent_request_id": body["request_id"],
                "question": "后来呢？",
            },
        )
        assert follow.status_code == 200 and follow.json()["usedContext"]
        foreign = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        async with foreign:
            await foreign.get("/api/session")
            assert (await foreign.get(answer["events"][0]["evidence"][0]["uri"])).status_code == 404
        with app.state.agent.ledger._connect() as conn:
            assert conn.execute("SELECT count(*) FROM attempts WHERE state='completed'").fetchone()[
                0
            ] == len(calls)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("consent", "tokenfactory_media_not_enabled"),
        ("host", "tokenfactory_endpoint_not_allowed"),
        ("http", "tokenfactory_endpoint_not_allowed"),
        ("model", "tokenfactory_model_not_allowed"),
        ("asr", "tokenfactory_asr_not_supported"),
        ("key", "model_key_missing"),
    ],
)
def test_configuration_guards(monkeypatch, mutation, code):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    config = configuration()
    assert not config.blockers()
    if mutation == "consent":
        config.allow_tokenfactory_media = False
    if mutation == "host":
        config.vision.base_url = BASE_URL + "/redirect"
    if mutation == "http":
        config.text.base_url = BASE_URL.replace("https:", "http:")
    if mutation == "model":
        config.vision.model = "other/vision"
    if mutation == "asr":
        config.asr = config.vision
    if mutation == "key":
        config.text.key_env = None
    assert code in config.blockers()
    assert not readiness_report(Settings(mode="real"), config, True)["dispatch_prerequisites_met"]


def test_no_approval_no_dispatch(tmp_path, monkeypatch):
    calls = []
    p = provider(tmp_path, monkeypatch, handler(calls))
    with pytest.raises(BudgetDenied):
        p.chat("text", "{}", **kwargs())
    assert not calls


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.jpg",
        "data:image/png;base64,AAAA",
        "data:image/jpeg;base64,AAAA",
        "data:video/mp4;base64,AAAA",
    ],
)
def test_only_jpeg_before_reservation(tmp_path, monkeypatch, url):
    calls = []
    p = provider(tmp_path, monkeypatch, handler(calls))
    approve(p.ledger, "owner")
    with pytest.raises(AgentError, match="tokenfactory_inline_jpeg_required"):
        p.chat("vision", [{"type": "image_url", "image_url": {"url": url}}], **kwargs())
    assert not calls and p.ledger.read_attempt("req_tf", "owner") is None


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", None])
def test_incomplete_response_unknown_no_retry(tmp_path, monkeypatch, finish):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": finish, "message": {"content": "{}"}}]}
        )

    p = provider(tmp_path, monkeypatch, respond)
    approve(p.ledger, "owner")
    with pytest.raises(AgentError, match="model_unknown"):
        p.chat("text", "{}", **kwargs())
    with pytest.raises(AgentError, match="unknown_attempt_do_not_resend"):
        p.chat("text", "{}", **kwargs())
    assert len(calls) == 1
    assert p.ledger.read_attempt("req_tf", "owner")["state"] == "unknown"


@pytest.mark.parametrize("failure", ["http503", "timeout", "length", "bad_json", "output_limit"])
def test_private_failure_diagnostics_keep_cause_without_payload(
    tmp_path, monkeypatch, caplog, failure
):
    calls = []

    def respond(req):
        calls.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-prompt mock-secret", request=req)
        if failure == "http503":
            return httpx.Response(503, headers={"x-request-id": "a" * 32}, text="secret-prompt")
        if failure == "bad_json":
            return httpx.Response(200, text="secret-prompt mock-secret")
        if failure == "output_limit":
            return httpx.Response(200, content=b"x" * (4 * 1024**2 + 1))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": "secret-prompt",
                            "reasoning_content": "private-reasoning",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
            },
        )

    p = provider(tmp_path, monkeypatch, respond)
    p.cache_dir = tmp_path / "model-results"
    approve(p.ledger, "owner")
    with caplog.at_level(logging.INFO, logger="frameseek.agent"):
        with pytest.raises(AgentError, match="model_unknown"):
            p.chat("text", "secret-prompt", **kwargs())
    files = list(p.cache_dir.glob("*.diagnostic.json"))
    assert len(files) == 1
    diagnostic = json.loads(files[0].read_text())
    assert (
        diagnostic["error"]
        == {
            "http503": "http_status",
            "timeout": "timeout",
            "length": "tokenfactory_incomplete_response",
            "bad_json": "response_json_invalid",
            "output_limit": "model_output_limit",
        }[failure]
    )
    if failure == "http503":
        assert diagnostic["http_status"] == 503
        assert diagnostic["upstream_request_id"] == "a" * 32
    if failure == "length":
        assert diagnostic["finish_reason"] == "length"
        assert diagnostic["usage"]["completion_tokens"] == 4096
    assert files[0].stat().st_mode & 0o777 == 0o600
    combined = files[0].read_text() + caplog.text
    assert not any(
        secret in combined for secret in ("secret-prompt", "mock-secret", "private-reasoning")
    )
    with pytest.raises(AgentError, match="unknown_attempt_do_not_resend"):
        p.chat("text", "secret-prompt", **kwargs())
    assert len(calls) == 1
    assert files[0].read_text() == json.dumps(diagnostic, sort_keys=True)
    assert p.ledger.read_attempt("req_tf", "owner")["state"] == "unknown"


@pytest.mark.parametrize("write_failure", [False, True])
def test_diagnostics_safe_metadata_and_disk_failure_do_not_replay_success(
    tmp_path, monkeypatch, caplog, write_failure
):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(
            200,
            headers={"x-request-id": "mock-secret"},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"ok":true}',
                            "reasoning_content": "private-reasoning",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": "mock-secret",
                    "total_tokens": -1,
                    "completion_tokens": 3,
                    "extra": "private-reasoning",
                },
            },
        )

    p = provider(tmp_path, monkeypatch, respond)
    p.cache_dir = tmp_path / "model-results"
    approve(p.ledger, "owner")
    replace = Path.replace

    def replace_without_diagnostic(path, target):
        if str(target).endswith(".diagnostic.json"):
            raise OSError("private-secret-path")
        return replace(path, target)

    if write_failure:
        monkeypatch.setattr(Path, "replace", replace_without_diagnostic)
    with caplog.at_level(logging.INFO, logger="frameseek.agent"):
        assert p.chat("text", "secret-prompt", **kwargs()) == {"ok": True}
        assert p.chat("text", "secret-prompt", **kwargs()) == {"ok": True}
    assert len(calls) == 1
    assert p.ledger.read_attempt("req_tf", "owner")["state"] == "completed"
    assert not any(
        secret in caplog.text
        for secret in ("mock-secret", "private-reasoning", "secret-prompt", "private-secret-path")
    )
    if write_failure:
        assert "model_diagnostic_write_failed" in caplog.text
    else:
        diagnostic = json.loads(next(p.cache_dir.glob("*.diagnostic.json")).read_text())
        assert "upstream_request_id" not in diagnostic
        assert diagnostic["usage"] == {"completion_tokens": 3}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["http503", "length"])
async def test_full_visual_failure_keeps_diagnosis_and_unknown_without_replay(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    working = handler(calls)
    fail_review = False

    def respond(req):
        payload = json.loads(req.content)
        content = payload.get("messages", [{}, {}])[-1].get("content")
        if fail_review and isinstance(content, list):
            assert sum(item["type"] == "image_url" for item in content) == 8
            calls.append((str(req.url), payload))
            if failure == "http503":
                return httpx.Response(503, headers={"x-request-id": "b" * 32})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "length", "message": {"content": '{"observations":['}}
                    ],
                    "usage": {"completion_tokens": 4096},
                },
            )
        return working(req)

    app, client, video, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=respond
    )
    try:
        fail_review = True
        before = len(calls)
        body = dict(
            investigation_id=desk["id"],
            request_id="req_visual_failure",
            generation=0,
            question="描述画面中的物体位置，并提供画面证据。",
        )
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 409
        assert response.json()["state"] == "unknown" and "response" not in response.json()
        assert len(calls) - before == 4
        diagnostic = [
            json.loads(p.read_text())
            for p in (app.state.workspace.root / video / "model-results").glob("*.diagnostic.json")
        ]
        failed = [item for item in diagnostic if item.get("error")]
        assert len(failed) == 1 and failed[0]["role"] == "vision"
        assert failed[0]["error"] == (
            "http_status" if failure == "http503" else "tokenfactory_incomplete_response"
        )
        assert not (app.state.workspace.root / video / "req_visual_failure_verified.json").exists()
        count = len(calls)
        repeated = await client.post("/api/chat", json=body)
        assert repeated.status_code == 409 and repeated.json()["state"] == "unknown"
        assert len(calls) == count
    finally:
        await client.aclose()


def test_no_redirect_or_auth_forwarding(tmp_path, monkeypatch):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(307, headers={"location": "https://other.example/collect"})

    p = provider(tmp_path, monkeypatch, respond)
    approve(p.ledger, "owner")
    with pytest.raises(AgentError, match="model_unknown"):
        p.chat("text", "{}", **kwargs())
    assert len(calls) == 1


def test_bounds_and_text_roles_cannot_send_images(tmp_path, monkeypatch):
    calls = []
    p = provider(tmp_path, monkeypatch, handler(calls))
    content = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,"
                + base64.b64encode(b"\xff\xd8\xff\xff\xd9").decode()
            },
        }
    ]
    with pytest.raises(AgentError, match="inline_jpeg"):
        p.chat("text", content, **kwargs())
    with pytest.raises(AgentError, match="frame_limit"):
        p.chat("vision", content * 129, **kwargs())
    for texts in [["x"] * 101, ["x" * 8001]]:
        with pytest.raises(AgentError, match="embedding_input_limit"):
            prepare_request(
                "embed", "/embeddings", {"model": EMBED_MODEL, "input": texts}, "ingest"
            )
    monkeypatch.setattr("frameseek.tokenfactory.MAX_REQUEST_BYTES", 100)
    with pytest.raises(AgentError, match="request_limit"):
        p.chat("text", "x" * 101, **kwargs())
    assert not calls and p.ledger.read_attempt("req_tf", "owner") is None


def test_template_blocked_and_legacy_checkpoints(tmp_path):
    runtime = Runtime.model_validate_json(
        Path("docs/runtime.tokenfactory.example.json").read_text()
    )
    assert "calibration_expired" in runtime.blockers()
    assert "resource_cost_bound_unverified" in runtime.blockers()
    legacy = configuration().model_dump()
    legacy["provider"] = "nvidia_nebius"
    legacy["allow_tokenfactory_media"] = False
    old = {k: v for k, v in legacy.items() if k != "allow_tokenfactory_media"}
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"runtime": old}))
    assert frozen_configuration_matches(path, {"runtime": legacy})
    legacy["allow_tokenfactory_media"] = True
    assert not frozen_configuration_matches(path, {"runtime": legacy})


def test_eight_image_limit_before_reservation(tmp_path, monkeypatch):
    calls = []
    p = provider(tmp_path, monkeypatch, handler(calls))
    approve(p.ledger, "owner")
    url = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xd9").decode()
    with pytest.raises(AgentError, match="frame_limit"):
        p.chat("vision", [{"type": "image_url", "image_url": {"url": url}}] * 9, **kwargs())
    assert not calls and p.ledger.read_attempt("req_tf", "owner") is None


def test_text_limit_before_reservation(tmp_path, monkeypatch):
    calls = []
    p = provider(tmp_path, monkeypatch, handler(calls))
    approve(p.ledger, "owner")
    with pytest.raises(AgentError, match="tokenfactory_text_limit"):
        p.chat("text", "中" * 7000, **kwargs())
    assert not calls and p.ledger.read_attempt("req_tf", "owner") is None


@pytest.mark.anyio
async def test_explicit_window_chat_fits_four_calls_and_keeps_real_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    app, client, video, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=handler(calls)
    )
    try:
        with app.state.agent.ledger._connect() as c:
            c.execute("UPDATE budgets SET call_cap=used_calls+4, money_cap=used_money+4")
        before = len(calls)
        body = dict(
            investigation_id=desk["id"],
            request_id="req_direct_window",
            generation=0,
            question="请查看视频00:05至00:11，描述纸箱的位置，并给出画面证据。",
        )
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["response"]["status"] in {"ok", "partial"}, result
        assert len(calls) - before == 2
        assert result["response"]["trace"]["visible"]["channels"] == []
        events = result["response"]["events"]
        assert events and all(e["start"] == 5 and e["end"] == 11 for e in events)
        for event in events:
            for frame in event["evidence"]:
                assert 5 <= frame["t"] < 11
                assert (await client.get(frame["uri"])).status_code == 200
        content = calls[before][1]["messages"][1]["content"]
        assert isinstance(content, list)
        assert sum(i["type"] == "image_url" for i in content) <= 8
        assert (await client.post("/api/chat", json=body)).json() == result
        assert len(calls) - before == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "question,expected",
    [
        ("请查看05:20至05:40的画面", {"start": 320, "end": 340}),
        ("请查看5:20–5:40的画面", {"start": 320, "end": 340}),
        ("Find a box from 00:04 to 00:06", {"start": 4, "end": 6}),
        ("Find a box BETWEEN 00:04 AND 00:06", {"start": 4, "end": 6}),
        ("在05:09找出穿蓝色球衣的人", {"start": 309, "end": 310}),
        ("Find a box at 00:04", {"start": 4, "end": 5}),
        ("Find a box at 00:04 or before", None),
        ("在00:04之前找箱子", None),
        ("在00:04左右找箱子", None),
        ("在00:04附近找箱子", None),
        ("不在00:04的箱子", None),
        ("Find a box not at 00:04", None),
        ('Find text "at 00:04"', None),
        ("Compare 00:04 and 00:06", None),
        ('Find text "00:04 to 00:06"', None),
        ('Find text "00:04 to 00:06" from 00:10 to 00:12', {"start": 10, "end": 12}),
        ("查找文字‘00:04至00:06’，请在00:10至00:12查看", {"start": 10, "end": 12}),
        ("从00:00到02:00", None),
        ("1:05:20至1:05:40", None),
        ("那后来呢？", None),
    ],
)
def test_explicit_window_parser_is_bounded_and_unambiguous(question, expected):
    from frameseek.agent import explicit_time_window

    assert explicit_time_window(question, 400) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["timeout", "bad_reference", "budget"])
async def test_direct_window_failure_is_honest_and_never_replays(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls = []
    working = handler(calls)
    reviewing = False

    def respond(req):
        if reviewing:
            payload = json.loads(req.content)
            calls.append((str(req.url), payload))
            if failure == "timeout":
                raise httpx.ReadTimeout("mock timeout")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "observations": [
                                            {"frame_ids": ["invented"], "statement": "unsupported"}
                                        ]
                                    }
                                )
                            },
                        }
                    ]
                },
            )
        return working(req)

    app, client, video, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=respond
    )
    try:
        reviewing = True
        with app.state.agent.ledger._connect() as c:
            c.execute(
                "UPDATE budgets SET call_cap=used_calls+?", (2 if failure == "budget" else 4,)
            )
        before = len(calls)
        body = dict(
            investigation_id=desk["id"],
            request_id="direct_failure",
            generation=0,
            question="请查看视频00:05至00:11，描述纸箱的位置。",
        )
        response = await client.post("/api/chat", json=body)
        result = response.json()
        if failure == "timeout":
            assert result["state"] == "unknown"
        else:
            assert result["response"]["status"] == "error"
            assert result["response"]["events"] == []
            if failure == "budget":
                assert response.status_code == 409
                assert result["response"]["errors"][0]["code"] == "budget_blocked"
                assert "预算" in result["response"]["answer"]
        assert len(calls) - before == (0 if failure == "budget" else 1)
        repeated = (await client.post("/api/chat", json=body)).json()
        if failure == "timeout":
            assert repeated["state"] == "unknown"
        else:
            assert repeated == result
        assert len(calls) - before == (0 if failure == "budget" else 1)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_direct_window_one_json_repair_stays_within_three_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    calls, saved = [], {}
    working = handler(calls)
    reviewing = False

    def respond(req):
        payload = json.loads(req.content)
        content = payload.get("messages", [{}, {}])[-1].get("content")
        if reviewing and isinstance(content, list):
            calls.append((str(req.url), payload))
            saved["frame"] = json.loads(content[1]["text"])["frame_id"]
            text = '{"observations":['
        elif (
            reviewing and isinstance(content, str) and content.startswith("Repair JSON syntax only")
        ):
            calls.append((str(req.url), payload))
            text = json.dumps(
                {
                    "observations": [
                        {"frame_ids": [saved["frame"]], "statement": "纸箱位于画面中央。"}
                    ]
                }
            )
        else:
            return working(req)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
        )

    app, client, video, desk = await setup_real(
        tmp_path, monkeypatch, calls, runtime_factory=configuration, response_handler=respond
    )
    try:
        reviewing = True
        with app.state.agent.ledger._connect() as c:
            c.execute("UPDATE budgets SET call_cap=used_calls+3, money_cap=used_money+3")
        before = len(calls)
        result = (
            await client.post(
                "/api/chat",
                json=dict(
                    investigation_id=desk["id"],
                    request_id="direct_repair",
                    generation=0,
                    question="请查看视频00:05至00:11，描述纸箱的位置。",
                ),
            )
        ).json()
        assert result["response"]["events"], result
        assert len(calls) - before == 3
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "question",
    [
        "05:40至05:20",
        "05:20至07:00",
        "05:20至05:99",
        "05:99至06:00",
        "05:20至05:40，对比06:00",
        "00:01至00:02和00:03至00:04",
        "between 00:02 and 00:69",
        "在05:09找人，再对比05:12",
    ],
)
def test_invalid_explicit_window_rejected(question):
    from frameseek.agent import explicit_time_window

    with pytest.raises(AgentError, match="invalid_time_window"):
        explicit_time_window(question, 400)


def test_query_disables_reasoning_without_raising_output_or_spending_limits():
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": "Return JSON only"},
            {"role": "user", "content": "{}"},
        ],
    }
    _, query = prepare_request("text", "/chat/completions", payload, "query")
    assert query["reasoning_effort"] == "none"
    assert query["max_completion_tokens"] == 4096
    assert query["service_tier"] == "default"
    _, ingest = prepare_request("text", "/chat/completions", payload, "ingest")
    assert "reasoning_effort" not in ingest
