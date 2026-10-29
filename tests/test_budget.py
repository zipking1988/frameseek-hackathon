import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from frameseek.attempts import ApprovedBudget, AttemptLedger, BudgetDenied


def install(ledger, scope, *, money=100, calls=40, primary=32):
    ledger.install_approved_record(
        ApprovedBudget(
            scope, "test-v1", "synthetic-test-not-human-spend", primary, calls, money, "USD_MICRO"
        )
    )


@pytest.fixture
def ledger(tmp_path):
    result = AttemptLedger(tmp_path / "ledger.db")
    install(result, "*")
    install(result, "owner1")
    return result


def reserve(ledger, id="a1", owner="owner1", money=100, **changes):
    kwargs = dict(
        attempt_id=id,
        owner=owner,
        version="test-v1",
        payload_hash="test-payload",
        kind="coarse",
        money_units=money,
        currency_unit="USD_MICRO",
    )
    return ledger.reserve(**{**kwargs, **changes})


def test_unknown_stays_reserved_across_restart_and_cannot_dispatch(ledger):
    reserve(ledger)
    ledger.transition("a1", "dispatched")
    ledger.transition("a1", "unknown")
    restarted = AttemptLedger(ledger.path)
    assert reserve(restarted) == "unknown"
    with pytest.raises(BudgetDenied):
        restarted.transition("a1", "dispatched")
    with pytest.raises(BudgetDenied):
        reserve(restarted, id="a2", money=1)
    assert restarted.read_attempt("a1", "other") is None


def test_concurrent_last_credit_reserves_only_one_attempt(ledger):
    barrier = Barrier(2)

    def contender(id):
        barrier.wait()
        try:
            return reserve(ledger, id=id)
        except BudgetDenied:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        states = list(executor.map(contender, ("a1", "a2")))
    assert sorted(states) == ["denied", "reserved"]
    assert sum(ledger.read_attempt(id, "owner1") is not None for id in ("a1", "a2")) == 1


def test_failed_owner_reserve_rolls_back_global_and_cookie_cannot_bypass(ledger):
    with pytest.raises(BudgetDenied):
        reserve(ledger, id="bad", owner="not-approved")
    assert reserve(ledger) == "reserved"
    install(ledger, "owner2")
    with pytest.raises(BudgetDenied):
        reserve(ledger, id="a2", owner="owner2", money=1)


def test_lower_or_revoked_approval_prevents_reserved_dispatch(ledger):
    reserve(ledger, money=80)
    ledger.restrict("*", "test-v1", money_units=79)
    with pytest.raises(BudgetDenied):
        ledger.transition("a1", "dispatched")
    with pytest.raises(BudgetDenied):
        ledger.restrict("*", "test-v1", money_units=100)
    ledger.restrict("owner1", "test-v1")
    with pytest.raises(BudgetDenied):
        reserve(ledger, id="a2", money=1)


def test_same_id_reuses_state_but_different_payload_or_currency_fails(ledger):
    reserve(ledger)
    assert reserve(ledger) == "reserved"
    with pytest.raises(BudgetDenied):
        reserve(ledger, payload_hash="changed")
    with pytest.raises(BudgetDenied):
        reserve(ledger, id="a2", money=1, currency_unit="EUR_MICRO")


def test_unconfirmed_30_and_confirmed_actual_n_transition(ledger):
    ledger.admit_windows("owner1", None, 30)
    for count in (31, 32, 33):
        with pytest.raises(BudgetDenied):
            ledger.admit_windows("owner1", None, count)
    ledger.admit_windows("owner1", "test-v1", 32)
    with pytest.raises(BudgetDenied):
        ledger.admit_windows("owner1", "test-v1", 33)


@pytest.mark.parametrize("money", [0, -1, True, 1.5, float("nan"), float("inf"), 2**53])
def test_invalid_or_unknown_money_never_reserves(ledger, money):
    with pytest.raises(BudgetDenied):
        reserve(ledger, money=money)
    assert ledger.read_attempt("a1", "owner1") is None


def test_all_stage_requests_share_total_call_budget(tmp_path):
    ledger = AttemptLedger(tmp_path / "ledger.db")
    for scope in ("*", "owner1"):
        install(ledger, scope, calls=7, primary=1)
    for n, kind in enumerate(("coarse", "zoom", "asr", "embed", "rerank", "text", "allocation")):
        reserve(ledger, id=f"a{n}", money=1, kind=kind)
    with pytest.raises(BudgetDenied):
        reserve(ledger, id="over", money=1, kind="text")


def test_reserved_crash_can_be_marked_unknown_without_releasing_money(ledger):
    reserve(ledger)
    ledger.transition("a1", "unknown")
    assert reserve(ledger) == "unknown"
    with pytest.raises(BudgetDenied):
        reserve(ledger, id="retry", money=1)


def test_incompatible_database_does_not_silently_rebuild(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(BudgetDenied, match="migration required"):
        AttemptLedger(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99


def test_existing_unversioned_database_requires_explicit_migration(tmp_path):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE foreign_data(id TEXT)")
    with pytest.raises(BudgetDenied, match="explicit migration"):
        AttemptLedger(path)


def settlement(**changes):
    return dict(
        attempt_id="a1",
        owner="owner1",
        version="test-v1",
        payload_hash="test-payload",
        money_units=10,
        currency_unit="USD_MICRO",
        reference="verified-provider-usage-bound",
        **changes,
    )


def budget_rows(ledger):
    with ledger._connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM budgets ORDER BY scope")]


def test_completed_settlement_is_idempotent_keeps_identity_calls_and_revocation(ledger):
    reserve(ledger, money=100)
    ledger.transition("a1", "dispatched")
    ledger.transition("a1", "completed")
    ledger.restrict("*", "test-v1")
    ledger.restrict("owner1", "test-v1")
    ledger.settle_completed(**settlement())
    ledger.settle_completed(**settlement())
    for b in budget_rows(ledger):
        assert (b["active"], b["used_calls"], b["used_primary"], b["used_money"]) == (0, 1, 1, 10)
        assert b["money_cap"] == 100
    assert reserve(ledger) == "completed"
    with pytest.raises(BudgetDenied):
        ledger.settle_completed(**{**settlement(), "money_units": 9})
    restarted = AttemptLedger(ledger.path)
    restarted.resume_existing("owner1", "test-v1", "human-resume-without-more-budget")
    for b in budget_rows(restarted):
        assert b["active"] == 1 and b["used_money"] == 10 and b["used_calls"] == 1
        assert b["money_cap"] == 100 and b["call_cap"] == 40
    reserve(restarted, id="a2", money=90)
    with pytest.raises(BudgetDenied):
        reserve(restarted, id="a3", money=1)


@pytest.mark.parametrize("state", ["reserved", "dispatched", "unknown"])
def test_non_completed_settlement_cannot_release_budget(ledger, state):
    reserve(ledger)
    if state != "reserved":
        ledger.transition("a1", state)
    before = budget_rows(ledger)
    with pytest.raises(BudgetDenied):
        ledger.settle_completed(**settlement())
    assert budget_rows(ledger) == before


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "foreign"},
        {"payload_hash": "different"},
        {"currency_unit": "EUR_MICRO"},
        {"version": "other"},
        {"money_units": 101},
        {"money_units": -1},
        {"money_units": True},
        {"reference": ""},
    ],
)
def test_settlement_requires_matching_identity_and_finite_downward_bound(ledger, change):
    reserve(ledger)
    ledger.transition("a1", "dispatched")
    ledger.transition("a1", "completed")
    before = budget_rows(ledger)
    with pytest.raises(BudgetDenied):
        ledger.settle_completed(**{**settlement(), **change})
    assert budget_rows(ledger) == before


def test_settlement_two_scope_failure_rolls_back(ledger):
    reserve(ledger)
    ledger.transition("a1", "dispatched")
    ledger.transition("a1", "completed")
    with ledger._connect() as c:
        c.execute("DELETE FROM budgets WHERE scope='owner1'")
    before = budget_rows(ledger)
    with pytest.raises(BudgetDenied):
        ledger.settle_completed(**settlement())
    assert budget_rows(ledger) == before
    with ledger._connect() as c:
        assert c.execute("SELECT count(*) FROM settlements").fetchone()[0] == 0


def test_concurrent_duplicate_settlement_reduces_once(ledger):
    reserve(ledger)
    ledger.transition("a1", "dispatched")
    ledger.transition("a1", "completed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: ledger.settle_completed(**settlement()), range(2)))
    assert all(b["used_money"] == 10 and b["used_calls"] == 1 for b in budget_rows(ledger))


def test_resume_missing_owner_does_not_activate_deployment(ledger):
    ledger.restrict("*", "test-v1")
    with pytest.raises(BudgetDenied):
        ledger.resume_existing("missing", "test-v1", "human")
    assert budget_rows(ledger)[0]["active"] == 0


def test_version_one_upgrade_keeps_approval_and_attempts(ledger):
    reserve(ledger)
    with ledger._connect() as c:
        c.execute("DROP TABLE settlements")
        c.execute("DROP TABLE resumptions")
        c.execute("PRAGMA user_version=1")
    upgraded = AttemptLedger(ledger.path)
    assert upgraded.read_attempt("a1", "owner1")["money"] == 100
    assert all(b["used_calls"] == 1 for b in budget_rows(upgraded))
    with upgraded._connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
