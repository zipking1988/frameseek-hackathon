"""Local ASGI proxy verification. No cloud resources, DNS, TLS or paid requests."""

import os
import time

import httpx
import pytest

from frameseek.api import create_app
from frameseek.config import Settings
from frameseek.gateway import create_gateway, https_origin
from frameseek.server import server_command

TOKEN = "synthetic-test-token-not-for-deployment-123456"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("FS_PUBLIC_ORIGIN", "https://public.example")
    monkeypatch.setenv("FS_BACKEND_ORIGIN", "https://backend.example")
    monkeypatch.setenv("FS_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))


@pytest.mark.anyio
async def test_gateway_preserves_secure_cookie_csrf_origin_and_private_range_media(
    monkeypatch, tmp_path
):
    configure(monkeypatch, tmp_path)
    backend = create_app(Settings())
    gateway = create_gateway(transport=httpx.ASGITransport(app=backend))
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="https://public.example"
        ) as client:
            session = await client.get("/api/session")
            assert session.status_code == 200
            assert "Secure" in session.headers["set-cookie"]
            assert "HttpOnly" in session.headers["set-cookie"]
            assert TOKEN not in session.text
            headers = {"x-fs-csrf": session.json()["csrf"], "origin": "https://public.example"}
            created = await client.post("/api/investigations", json={}, headers=headers)
            assert created.status_code == 201, created.text
            rejected = await client.post(
                "/api/investigations",
                json={},
                headers={**headers, "origin": "https://foreign.example"},
            )
            assert rejected.status_code == 403 and rejected.json()["detail"] == "origin_rejected"
            csrf = await client.post(
                "/api/investigations", json={}, headers={"origin": "https://public.example"}
            )
            assert csrf.status_code == 403
            # Use actual backend-owned uploaded source without decoder/network calls.
            owner = session.json()["owner_id"]
            video = "vid_gateway_test"
            with backend.state.workspace.db() as connection:
                connection.execute(
                    "INSERT INTO videos(id,owner,name,bytes,expires) VALUES(?,?,?,?,?)",
                    (video, owner, "private.mp4", 21, time.time() + 60),
                )
            directory = tmp_path / video
            directory.mkdir()
            (directory / "original").write_bytes(b"private-video-content")
            uri = f"/media/{video}/video"
            ranged = await client.get(uri, headers={"Range": "bytes=0-6"})
            assert ranged.status_code == 206 and ranged.content == b"private"
            assert ranged.headers["content-range"] == "bytes 0-6/21"
            # Another browser cookie has no access even behind the authenticated gateway.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway), base_url="https://public.example"
            ) as foreign:
                await foreign.get("/api/session")
                assert (await foreign.get(uri)).status_code == 404
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend), base_url="https://public.example"
        ) as direct:
            assert (await direct.get("/api/session")).status_code == 401
            assert (
                await direct.get("/api/session", headers={"x-fs-gateway-token": "spoofed"})
            ).status_code == 401


@pytest.mark.anyio
async def test_gateway_has_no_storage_replaces_spoofed_headers_and_preserves_cookies(
    monkeypatch, tmp_path
):
    configure(monkeypatch, tmp_path)
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["x-fs-gateway-token"] == TOKEN
        assert request.headers["host"] == "public.example"
        assert "forwarded" not in request.headers and "x-forwarded-for" not in request.headers
        return httpx.Response(
            200,
            headers=[("set-cookie", "one=1"), ("set-cookie", "two=2")],
            stream=httpx.ByteStream(b"ok"),
        )

    app = create_gateway(transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://public.example"
        ) as client:
            result = await client.get(
                "/api/test?owner=x",
                headers={
                    "x-fs-gateway-token": "spoofed",
                    "forwarded": "host=evil",
                    "x-forwarded-for": "fake",
                    "x-forwarded-host": "evil",
                },
            )
            assert result.content == b"ok"
            assert result.headers.get_list("set-cookie") == ["one=1", "two=2"]
            assert str(calls[0].url) == "https://backend.example/api/test?owner=x"
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_gateway_failure_is_explicit_and_does_not_retry(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    calls = []

    def upstream(request):
        calls.append(request)
        raise httpx.ConnectError("test failure")

    app = create_gateway(transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://public.example"
        ) as client:
            response = await client.get("/api/session")
            assert response.status_code == 502
            assert response.json() == {"detail": "workspace_backend_unavailable"}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        "http://host",
        "https://u:p@host",
        "https://host/path",
        "https://host?key=x",
        "https://host/#x",
        "https://host:bad",
    ],
)
def test_public_and_backend_origins_are_exact_https_origins(value):
    with pytest.raises(RuntimeError):
        https_origin(value)


def test_container_refuses_ephemeral_workspace_and_uses_one_process(monkeypatch):
    with pytest.raises(RuntimeError, match="Cloud Run"):
        server_command({"K_SERVICE": "test"})
    with pytest.raises(RuntimeError, match="PORT"):
        server_command({"PORT": "8080;echo"})
    command = server_command({"K_SERVICE": "test", "FS_COMPONENT": "gateway", "PORT": "9000"})
    assert "frameseek.gateway:create_gateway" in command
    assert command[command.index("--workers") + 1] == "1"
    assert command[command.index("--port") + 1] == "9000"
    monkeypatch.setattr("frameseek.server.os.path.ismount", lambda _: False)
    with pytest.raises(RuntimeError, match="volume mount"):
        server_command({"FS_REQUIRE_DATA_MOUNT": "1"})
    # The stateless component does not create or require a workspace volume.
    assert "frameseek.gateway:create_gateway" in server_command(
        {"FS_COMPONENT": "gateway", "FS_REQUIRE_DATA_MOUNT": "1"}
    )


def test_direct_factory_also_refuses_cloud_run_workspace_before_creating_data(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("K_SERVICE", "test")
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "must-not-exist"))
    with pytest.raises(RuntimeError, match="Cloud Run"):
        create_app(Settings())
    assert not os.path.exists(tmp_path / "must-not-exist")


@pytest.mark.anyio
async def test_gateway_streams_binary_upload_and_rejects_declared_and_streamed_limits(
    monkeypatch, tmp_path
):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr("frameseek.gateway.MAX_BODY_BYTES", 16)
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.content == b"\x00\xffvideo"
        return httpx.Response(
            201,
            stream=httpx.ByteStream(b'{"id":"test"}'),
            headers={"content-type": "application/json"},
        )

    app = create_gateway(transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://public.example"
        ) as client:
            result = await client.post(
                "/api/videos", content=b"\x00\xffvideo", headers={"content-type": "video/mp4"}
            )
            assert result.status_code == 201 and result.json() == {"id": "test"}
            assert (await client.post("/api/videos", content=b"x" * 17)).status_code == 413

            async def too_large():
                yield b"x" * 9
                yield b"x" * 9

            result = await client.post("/api/videos", content=too_large())
            assert result.status_code == 413
            assert result.json()["detail"] == "gateway_upload_limit_30_mib"
    assert len(calls) == 1
