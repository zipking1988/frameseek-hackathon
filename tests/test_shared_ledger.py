import pytest

from frameseek.attempts import ApprovedBudget, AttemptLedger, BudgetDenied
from frameseek.shared_ledger import SharedLedger, dispatch


def test_direct_observations_uses_existing_local_ledger(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from frameseek.shared_ledger import configured_ledger

    monkeypatch.delenv("FS_SHARED_LEDGER_SSH", raising=False)
    path = tmp_path / "central.sqlite3"
    original, _, _ = setup(tmp_path)
    with original._connect() as connection:
        before = connection.execute("SELECT * FROM budgets ORDER BY scope").fetchall()
    runtime = SimpleNamespace(provider="tokenfactory")
    ordinary = configured_ledger(path, runtime)
    assert not getattr(ordinary, "direct_observations", False)
    monkeypatch.setenv("FS_DIRECT_OBSERVATIONS", "1")
    direct = configured_ledger(path, runtime)
    assert direct.direct_observations and not direct.allow_repairs
    with direct._connect() as connection:
        assert connection.execute("SELECT * FROM budgets ORDER BY scope").fetchall() == before
    with pytest.raises(BudgetDenied, match="require Token Factory"):
        configured_ledger(path, SimpleNamespace(provider="gemini"))


def setup(tmp_path):
    ledger = AttemptLedger(tmp_path / "central.sqlite3")
    binding = {
        "remote_owner": "online",
        "local_owner": "local",
        "version": "approved",
        "call_cap": 40,
        "money_cap": 20_000_000,
    }
    for scope in ("*", "online"):
        ledger.install_approved_record(
            ApprovedBudget(
                scope, "approved", "synthetic-test-only", 40, 40, 20_000_000, "USD_MICRO"
            )
        )
    with ledger._connect() as conn:
        conn.execute("UPDATE budgets SET used_calls=35,used_money=884047")
    shared = SharedLedger("example", "local", "approved")
    shared._rpc = lambda method, **args: dispatch(ledger, binding, {"method": method, "args": args})
    return ledger, binding, shared


def reserve(shared, attempt="local-new", **changes):
    args = dict(
        attempt_id=attempt,
        owner="local",
        version="approved",
        payload_hash=attempt,
        kind="zoom",
        money_units=100,
        currency_unit="USD_MICRO",
    )
    return shared.reserve(**(args | changes))


def test_local_and_online_reservations_share_original_caps_and_history(tmp_path):
    ledger, _, shared = setup(tmp_path)
    for i in range(5):
        if i % 2:
            ledger.reserve(
                attempt_id=str(i),
                owner="online",
                version="approved",
                payload_hash=str(i),
                kind="text",
                money_units=100,
                currency_unit="USD_MICRO",
            )
        else:
            reserve(shared, str(i))
    with pytest.raises(BudgetDenied):
        reserve(shared, "over-cap")
    with shared._connect() as conn:
        assert all(r[0] == 40 for r in conn.execute("SELECT used_calls FROM budgets"))
        assert all(r[0] == 884547 for r in conn.execute("SELECT used_money FROM budgets"))
        assert conn.execute("SELECT count(*) FROM budgets WHERE scope='local'").fetchone()[0] == 1
        with pytest.raises(Exception, match="readonly"):
            conn.execute("UPDATE budgets SET used_calls=0")


def test_unknown_is_retained_owner_bound_and_no_administration_api(tmp_path):
    ledger, binding, shared = setup(tmp_path)
    reserve(shared)
    shared.transition("local-new", "dispatched")
    shared.transition("local-new", "unknown")
    assert shared.read_attempt("local-new", "local")["state"] == "unknown"
    assert reserve(shared) == "unknown"
    with pytest.raises(BudgetDenied):
        reserve(shared, owner="foreign")
    with pytest.raises(BudgetDenied):
        reserve(shared, version="other")
    for method in ("install_approved_record", "resume_existing", "settle_completed", "restrict"):
        with pytest.raises(BudgetDenied):
            dispatch(ledger, binding, {"method": method})
    with ledger._connect() as conn:
        assert conn.execute("SELECT used_calls FROM budgets WHERE scope='*'").fetchone()[0] == 36


def test_ssh_failure_is_closed_and_never_retried(monkeypatch):
    import subprocess

    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("ssh", 20)

    monkeypatch.setattr(subprocess, "run", fail)
    shared = SharedLedger("example", "local", "approved")
    with pytest.raises(BudgetDenied, match="no automatic retry"):
        shared.admit_cost("local", "approved", 3, 100, "USD_MICRO")
    assert len(calls) == 1


def test_local_shared_route_does_not_issue_json_repair_call(monkeypatch):
    from types import SimpleNamespace

    from frameseek.agent import AgentError, Provider

    provider = Provider(
        SimpleNamespace(vision=SimpleNamespace(model="synthetic")),
        SharedLedger("example", "local", "approved"),
        lambda: None,
    )
    calls = []

    def invalid(*args, **kwargs):
        calls.append(args)
        return {"choices": [{"message": {"content": "invalid-json"}}]}

    monkeypatch.setattr(provider, "call", invalid)
    with pytest.raises(AgentError, match="model_invalid_json"):
        provider.chat("vision", "question")
    assert len(calls) == 1


@pytest.mark.parametrize("repairs,ingest_calls,query_calls", [(False, 2, 7), (True, 3, 8)])
def test_admission_does_not_reserve_disabled_repairs(
    tmp_path, monkeypatch, repairs, ingest_calls, query_calls
):
    import json
    from types import SimpleNamespace

    from test_tokenfactory import configuration

    from frameseek.agent import Agent
    from frameseek.config import Settings

    monkeypatch.setenv("TEST_TF_KEY", "mock-secret")
    admitted = []
    agent = object.__new__(Agent)
    agent.runtime = configuration()
    agent.settings = Settings(mode="real")
    agent.ws = SimpleNamespace(
        root=tmp_path,
        owned=lambda *args: {
            "metadata": json.dumps({"windows": [{"start": 0, "end": 5}], "audio": False}),
            "generation": 1,
        },
    )
    agent.ledger = SimpleNamespace(
        allow_repairs=repairs,
        read_attempt=lambda *args: None,
        admit_windows=lambda *args: None,
        admit_cost=lambda *args: admitted.append(args),
    )
    agent.admit_ingest("video", "owner")
    assert admitted[0][2:4] == (ingest_calls, ingest_calls)
    _, calls, cost = agent.query_budget({"segments": [{}, {}, {}]}, {"duration": 60}, "描述场景")
    assert calls == cost == query_calls


def test_shared_visual_search_flag_rejects_wrong_provider_before_rpc(monkeypatch):
    from types import SimpleNamespace

    from frameseek.shared_ledger import configured_ledger

    monkeypatch.setenv("FS_SHARED_LEDGER_SSH", "example")
    monkeypatch.setenv("FS_SHARED_LEDGER_LOCAL_OWNER", "local")
    monkeypatch.setenv("FS_VISUAL_SEARCH", "1")
    monkeypatch.delenv("FS_PUBLIC_ORIGIN", raising=False)
    with pytest.raises(BudgetDenied, match="Visual search requires Token Factory"):
        configured_ledger(None, SimpleNamespace(provider="gemini", budget_version="mock"))
