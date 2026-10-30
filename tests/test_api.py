import socket

import pytest
from httpx import ASGITransport, AsyncClient

from frameseek.api import create_app
from frameseek.config import Settings
from frameseek.fixtures import examples
from frameseek.preflight import report


@pytest.mark.anyio
async def test_local_health_and_schema_do_not_claim_real_readiness():
    async with AsyncClient(
        transport=ASGITransport(app=create_app(Settings())), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["mode"] == "fixture"
        assert response.json()["real_ready"] is False
        assert response.json()["model_requests_sent"] == 0
        schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
        assert schemas["Answer"]["properties"]["status"]["enum"] == [
            "ok",
            "partial",
            "no_result",
            "error",
        ]
        assert "QueryControl" in schemas
        assert "Find the play." in (await client.get("/")).text
        assert "Investigation workspace" in (await client.get("/workspace")).text
        assert (await client.get("/docs/errors")).status_code == 200
        assert (await client.post("/api/chat", json={"question": "anything"})).status_code == 401


@pytest.mark.anyio
async def test_sports_landing_is_public_without_initializing_a_private_session():
    app = create_app(Settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "set-cookie" not in page.headers
        assert 'href="/workspace"' in page.text
        assert "Illustrative basketball preview · no model calls" in page.text
        assert "/static/workspace.js" not in page.text
        for file in ("courtside-hero.jpg", "shot.jpg", "court.jpg", "pass.jpg"):
            asset = await client.get(f"/static/landing/{file}")
            assert asset.status_code == 200
            assert asset.headers["content-type"] == "image/jpeg"
        assert (await client.get("/static/landing/filmroom-hero.png")).headers[
            "content-type"
        ] == "image/png"
        assert (await client.get("/static/landing/hoop-banner.png")).headers[
            "content-type"
        ] == "image/png"
        assert (await client.get("/api/investigations")).status_code == 401
        assert (await client.get("/health")).json()["model_requests_sent"] == 0


@pytest.mark.anyio
async def test_html_pages_do_not_reuse_stale_cached_shells():
    app = create_app(Settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/", "/workspace", "/static/landing.html", "/static/demo-guide-en.html"):
            first = await client.get(path)
            assert first.status_code == 200
            assert "no-store" in first.headers["cache-control"]
            stale = await client.get(
                path,
                headers={
                    "If-None-Match": first.headers["etag"],
                    "If-Modified-Since": "Wed, 31 Dec 2099 23:59:59 GMT",
                },
            )
            assert stale.status_code == 200
            assert stale.content == first.content


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_real_mode_fails_closed_without_creating_any_provider():
    with pytest.raises(RuntimeError, match="Real mode is blocked"):
        create_app(Settings(mode="real"))
    preflight = report(Settings(mode="real"))
    assert preflight["real_ready"] is False
    assert preflight["blockers"]
    assert preflight["gpu_resources_started"] == 0


def test_contract_fixtures_never_open_a_network_socket(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fixture attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert len(examples()) == 4


@pytest.mark.anyio
async def test_public_demo_has_real_local_assets_and_range_playback():
    from frameseek.schema import Answer

    async with AsyncClient(
        transport=ASGITransport(app=create_app(Settings())), base_url="http://test"
    ) as client:
        catalog = (await client.get("/demo/catalog")).json()
        assert catalog["synthetic"] and catalog["mode"] == "fixture"
        assert {q["response"]["status"] for q in catalog["queries"]} == {
            "ok",
            "partial",
            "no_result",
            "error",
        }
        for query in catalog["queries"]:
            answer = Answer.model_validate(query["response"])
            for event in answer.events:
                for evidence in event.evidence:
                    response = await client.get(evidence.uri)
                    assert response.status_code == 200
                    assert response.headers["content-type"] == "image/jpeg"
                    assert response.content.startswith(b"\xff\xd8")
        video = await client.get("/demo/video", headers={"Range": "bytes=0-31"})
        assert video.status_code == 206
        assert len(video.content) == 32 and "ftyp" in video.text
        assert (await client.get("/media/another_video/frames/6.jpg")).status_code == 401
        assert (await client.get("/static/%2e%2e/%2e%2e/config.py")).status_code == 404
