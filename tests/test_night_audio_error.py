"""A missing audio channel must be explained without any provider dispatch."""

import pytest
from test_agent import setup_real


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_audio_refusal_explains_no_dispatch_and_preserves_ledger(tmp_path, monkeypatch):
    calls = []
    app, client, _, desk = await setup_real(tmp_path, monkeypatch, calls)
    before = len(calls)
    with app.state.agent.ledger._connect() as c:
        attempts = c.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    try:
        result = await client.post(
            "/api/chat",
            json={
                "investigation_id": desk["id"],
                "request_id": "audio_refusal",
                "generation": 0,
                "question": "解说员说了什么？请依据音轨回答。",
            },
        )
        answer = result.json()["response"]
        assert answer["status"] == "error"
        assert "audio has not been reliably transcribed" in answer["answer"]
        assert "No model was called" in answer["answer"]
        assert answer["events"] == []
        assert answer["errors"][0]["code"] == "audio_channel_unavailable"
        assert len(calls) == before
        with app.state.agent.ledger._connect() as c:
            assert c.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == attempts
    finally:
        await client.aclose()
