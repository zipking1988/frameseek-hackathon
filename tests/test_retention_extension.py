"""Explicit retention preserves existing private ownership; ordinary TTL stays finite."""

import math
import time

import pytest
from httpx import ASGITransport, AsyncClient

from frameseek.api import create_app
from frameseek.config import Settings
from frameseek.workspace import Workspace


@pytest.fixture
def anyio_backend():
    return "asyncio"


def source(ws, owner, key="vid_owned", **values):
    with ws.db() as c:
        c.execute(
            "INSERT INTO videos(id,owner,name,bytes,expires,deleted) VALUES(?,?,?,?,?,?)",
            (
                key,
                owner,
                "private.mp4",
                10,
                values.get("expires", time.time() + 86400),
                values.get("deleted", 0),
            ),
        )
    return key


def test_extension_scopes_current_future_desks_without_changing_other_retention(tmp_path):
    ws = Workspace(tmp_path)
    owner = ws.session()[0]["id"]
    other = ws.session()[0]["id"]
    video = source(ws, owner)
    ordinary = source(ws, owner, "vid_ordinary")
    desk = ws.create(owner, video)
    normal_desk = ws.create(owner, ordinary)
    until = time.time() + 80 * 86400
    ws.extend_retention(owner, video, until, "explicit-test-retention")
    ws.extend_retention(owner, video, until, "explicit-test-retention")
    assert ws.owned("videos", video, owner)["expires"] == until
    assert ws.get(desk["id"], owner)["expires"] == until
    assert ws.create(owner, video)["expires"] == until
    assert ws.get(normal_desk["id"], owner)["expires"] < until
    assert ws.owned("videos", ordinary, owner)["expires"] < until
    with ws.db() as c:
        assert c.execute("SELECT expires FROM sessions WHERE id=?", (owner,)).fetchone()[0] == until
        assert c.execute("SELECT expires FROM sessions WHERE id=?", (other,)).fetchone()[0] < until
        assert c.execute("SELECT count(*) FROM retention_extensions").fetchone()[0] == 1
    with pytest.raises(ValueError, match="already used"):
        ws.extend_retention(owner, ordinary, until, "explicit-test-retention")


@pytest.mark.parametrize("until", [math.inf, math.nan, True, 0, time.time() + 121 * 86400])
def test_invalid_extension_cutoffs_are_rejected(tmp_path, until):
    ws = Workspace(tmp_path)
    owner = ws.session()[0]["id"]
    with pytest.raises(ValueError):
        ws.extend_retention(owner, source(ws, owner), until, "invalid")


@pytest.mark.parametrize(
    "expired,deleted,wrong_owner",
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_extension_cannot_revive_or_reassign_a_source(tmp_path, expired, deleted, wrong_owner):
    ws = Workspace(tmp_path)
    owner = ws.session()[0]["id"]
    caller = ws.session()[0]["id"] if wrong_owner else owner
    video = source(
        ws, owner, expires=time.time() - 1 if expired else time.time() + 86400, deleted=int(deleted)
    )
    with pytest.raises(ValueError):
        ws.extend_retention(caller, video, time.time() + 80 * 86400, "rejected")
    with ws.db() as c:
        assert c.execute("SELECT count(*) FROM retention_extensions").fetchone()[0] == 0


@pytest.mark.anyio
async def test_cookie_refresh_uses_existing_server_cutoff_and_never_rolls_it(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))
    app = create_app(Settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        await client.get("/api/session")
        token = client.cookies.get("fs_session")
        ws = app.state.workspace
        owner = ws.authenticate(token)["id"]
        until = time.time() + 80 * 86400
        ws.extend_retention(owner, source(ws, owner), until, "explicit-cookie-test")
        response = await client.get("/api/session")
        assert client.cookies.get("fs_session") == token
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
        max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
        assert abs(max_age - 80 * 86400) < 3
        assert ws.authenticate(token)["expires"] == until
        assert (await client.post("/api/retention", json={"until": until})).status_code == 404


def test_operator_cli_refuses_running_workspace_before_lifecycle_recovery(tmp_path, monkeypatch):
    import fcntl
    import os
    import sys

    from frameseek.workspace_admin import main

    ws = Workspace(tmp_path)
    owner = ws.session()[0]["id"]
    video = source(ws, owner)
    lock = os.open(tmp_path / "serve.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "workspace_admin",
                "--data-dir",
                str(tmp_path),
                "--owner",
                owner,
                "--video",
                video,
                "--until",
                "2027-01-13T00:00:00+09:00",
                "--confirm-authorized-reference",
                "test",
            ],
        )
        with pytest.raises(SystemExit) as result:
            main()
        assert result.value.code == 2
        with ws.db() as c:
            assert c.execute("SELECT count(*) FROM retention_extensions").fetchone()[0] == 0
    finally:
        os.close(lock)
