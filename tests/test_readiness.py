import asyncio
import json
import subprocess
import time

from httpx import ASGITransport, AsyncClient

from frameseek.agent import Endpoint, Runtime
from frameseek.api import create_app
from frameseek.attempts import ApprovedBudget, AttemptLedger
from frameseek.config import Settings
from frameseek.readiness import budget_ready, readiness_report


def runtime():
    endpoint = Endpoint(
        base_url="http://127.0.0.1:9000/v1",
        model="nvidia/test",
        revision="MOCK-NOT-LIVE",
        key_env="TEST_READINESS_KEY",
        max_cost_micro=1,
    )
    return Runtime(
        vision=endpoint,
        embed=endpoint,
        rerank=endpoint,
        text=endpoint,
        budget_version="test",
        calibration_reference="TEST ONLY",
        resource_cutoff_reference="TEST ONLY",
        valid_until=time.time() + 300,
        attempt_seconds=2,
        self_hosted_hosts=["127.0.0.1"],
    )


def test_readiness_does_not_claim_live_verification_or_leak_credentials(monkeypatch):
    monkeypatch.setenv("TEST_READINESS_KEY", "synthetic-secret-do-not-expose")
    monkeypatch.setattr("frameseek.readiness.shutil.which", lambda tool: "/test/" + tool)
    report = readiness_report(Settings(mode="real"), runtime(), approved=True)
    assert report["dispatch_prerequisites_met"]
    assert not report["live_verified"]
    assert report["model_requests_sent_by_check"] == 0
    assert "synthetic-secret-do-not-expose" not in json.dumps(report)
    assert "base_url" not in json.dumps(report)
    assert report["models"][-1]["optional"] and not report["models"][-1]["configured"]


def test_fixture_missing_keys_calibration_and_budget_are_distinct_gates(monkeypatch):
    monkeypatch.delenv("TEST_READINESS_KEY", raising=False)
    config = runtime()
    config.valid_until = time.time() - 1
    report = readiness_report(Settings(), config)
    gates = {r["id"]: r["state"] for r in report["checks"]}
    assert gates["mode"] == gates["models"] == gates["calibration"] == gates["budget"] == "blocked"
    assert not report["dispatch_prerequisites_met"]


def test_budget_readiness_requires_both_active_same_version_scopes(tmp_path):
    ledger = AttemptLedger(tmp_path / "attempts.sqlite3")
    config = runtime()
    ledger.install_approved_record(ApprovedBudget("*", "test", "TEST ONLY", 1, 2, 2, "USD_MICRO"))
    with ledger._connect() as connection:
        assert not budget_ready(connection, "owner", config)
    ledger.install_approved_record(
        ApprovedBudget("owner", "test", "TEST ONLY", 1, 2, 2, "USD_MICRO")
    )
    with ledger._connect() as connection:
        assert budget_ready(connection, "owner", config)
    ledger.restrict("*", "test")
    with ledger._connect() as connection:
        assert not budget_ready(connection, "owner", config)


def test_owned_status_is_read_only_and_not_public(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path))
    app = create_app(Settings())

    async def verify():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/runtime/status")).status_code == 401
            await client.get("/api/session")
            response = await client.get("/api/runtime/status")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["mode"] == "fixture"
            assert not response.json()["dispatch_prerequisites_met"]
            with app.state.agent.ledger._connect() as connection:
                assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM budgets").fetchone()[0] == 0

    asyncio.run(verify())


def test_cli_does_not_create_private_storage_for_a_check(tmp_path, monkeypatch):
    import sys

    directory = tmp_path / "does-not-exist"
    completed = subprocess.run(
        [sys.executable, "-m", "frameseek.readiness", "--data-dir", str(directory)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["model_requests_sent_by_check"] == 0
    assert not directory.exists()
