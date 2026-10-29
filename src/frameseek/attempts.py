"""Local budget ledger foundation; no provider calls or public approval API.

Trusted approved records -> BEGIN IMMEDIATE -> reserve owner + deployment
                         -> dispatch intent -> completed | unknown
Unknown keeps its reservation. A same-ID replay returns the existing attempt.
"""

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class BudgetDenied(ValueError):
    pass


def finite_integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise BudgetDenied(f"{name} must be a bounded integer")


@dataclass(frozen=True)
class ApprovedBudget:
    """Internal trusted-server input, not a user/env/config approval mechanism.

    D1 tests install synthetic records. Real admission remains disabled until
    the human approval workflow, pricing and resource cutoff are verified.
    Money uses one explicitly named integer unit, e.g. USD_MICRO.
    """

    scope: str  # '*' deployment, otherwise an owner identifier
    version: str
    approval_reference: str
    primary_calls: int
    model_calls: int
    money_units: int
    currency_unit: str

    def __post_init__(self):
        if not all((self.scope, self.version, self.approval_reference)):
            raise BudgetDenied("Approval identifiers are required")
        if not re.fullmatch(r"[A-Z]{3}_MICRO", self.currency_unit):
            raise BudgetDenied("Expected explicit currency and integer micro unit")
        finite_integer(self.primary_calls, "primary_calls", minimum=1)
        finite_integer(self.model_calls, "model_calls", minimum=1)
        finite_integer(self.money_units, "money_units", minimum=1)
        if self.primary_calls > self.model_calls:
            raise BudgetDenied("Primary calls cannot exceed total model calls")


class AttemptLedger:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise BudgetDenied("Unknown ledger schema version; migration required")
            if (
                version == 0
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
            ):
                raise BudgetDenied("Unversioned existing database requires explicit migration")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS budgets (
                    scope TEXT NOT NULL, version TEXT NOT NULL, reference TEXT NOT NULL,
                    primary_cap INTEGER NOT NULL, call_cap INTEGER NOT NULL,
                    money_cap INTEGER NOT NULL, currency TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    used_primary INTEGER NOT NULL DEFAULT 0,
                    used_calls INTEGER NOT NULL DEFAULT 0,
                    used_money INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope, version)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, version TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, kind TEXT NOT NULL,
                    money INTEGER NOT NULL, currency TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK(state IN ('reserved','dispatched','completed','unknown'))
                );
                CREATE TABLE IF NOT EXISTS settlements (
                    attempt TEXT PRIMARY KEY REFERENCES attempts(id),
                    money INTEGER NOT NULL, reference TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resumptions (
                    owner TEXT NOT NULL, version TEXT NOT NULL, reference TEXT NOT NULL,
                    PRIMARY KEY(owner, version, reference)
                );
            """)
            conn.execute("PRAGMA user_version=2")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=1000")
            with conn:
                yield conn
        finally:
            conn.close()

    def install_approved_record(self, budget: ApprovedBudget) -> None:
        """For a future trusted approval service/tests; never expose as HTTP/config."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO budgets("
                "scope,version,reference,primary_cap,call_cap,money_cap,currency) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    budget.scope,
                    budget.version,
                    budget.approval_reference,
                    budget.primary_calls,
                    budget.model_calls,
                    budget.money_units,
                    budget.currency_unit,
                ),
            )

    def restrict(self, scope: str, version: str, *, money_units: int | None = None) -> None:
        """Revoke by default; a lower cap never refunds spent or reserved money."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT money_cap FROM budgets WHERE scope=? AND version=?", (scope, version)
            ).fetchone()
            if old is None:
                raise BudgetDenied("Unknown approved budget")
            if money_units is None:
                conn.execute(
                    "UPDATE budgets SET active=0 WHERE scope=? AND version=?", (scope, version)
                )
            else:
                finite_integer(money_units, "money_units")
                if money_units > old["money_cap"]:
                    raise BudgetDenied("Increasing a cap requires a new approved version")
                conn.execute(
                    "UPDATE budgets SET money_cap=? WHERE scope=? AND version=?",
                    (money_units, scope, version),
                )

    def settle_completed(
        self, *, attempt_id, owner, version, payload_hash, money_units, currency_unit, reference
    ):
        """Trusted operator only: retain an evidenced bound, never refund unknowns or calls.

        Keep the original attempt reservation for idempotency/fingerprints. The
        append-only settlement records the one permitted reduction. No HTTP path
        calls this method, and settlement never activates a revoked approval.
        """
        finite_integer(money_units, "settled money")
        if not isinstance(reference, str) or not reference.strip():
            raise BudgetDenied("Settlement evidence reference required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if (
                not row
                or row["state"] != "completed"
                or (row["owner"], row["version"], row["payload_hash"], row["currency"])
                != (owner, version, payload_hash, currency_unit)
                or money_units > row["money"]
            ):
                raise BudgetDenied("Only matching completed attempts can be settled downward")
            previous = conn.execute(
                "SELECT money,reference FROM settlements WHERE attempt=?", (attempt_id,)
            ).fetchone()
            if previous:
                if tuple(previous) != (money_units, reference):
                    raise BudgetDenied("Settlement already recorded with different evidence")
                return
            reduction = row["money"] - money_units
            for scope in ("*", owner):
                updated = conn.execute(
                    "UPDATE budgets SET used_money=used_money-? WHERE scope=? AND version=? "
                    "AND currency=? AND used_money>=?",
                    (reduction, scope, version, currency_unit, reduction),
                )
                if updated.rowcount != 1:
                    raise BudgetDenied("Missing or inconsistent settlement budget")
            conn.execute(
                "INSERT INTO settlements VALUES(?,?,?)", (attempt_id, money_units, reference)
            )

    def resume_existing(self, owner, version, reference):
        """Explicit operator authorization to resume the same caps and consumed counters."""
        if not owner or owner == "*" or not reference or not reference.strip():
            raise BudgetDenied("Resume requires owner and human authorization reference")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for scope in ("*", owner):
                row = conn.execute(
                    "SELECT * FROM budgets WHERE scope=? AND version=?", (scope, version)
                ).fetchone()
                if (
                    not row
                    or row["used_money"] > row["money_cap"]
                    or row["used_calls"] > row["call_cap"]
                    or row["used_primary"] > row["primary_cap"]
                    or conn.execute(
                        "SELECT 1 FROM budgets WHERE scope=? AND version!=? AND active=1",
                        (scope, version),
                    ).fetchone()
                ):
                    raise BudgetDenied("Cannot resume incompatible or exhausted approval")
            conn.execute(
                "UPDATE budgets SET active=1 WHERE version=? AND scope IN (?, '*')",
                (version, owner),
            )
            conn.execute(
                "INSERT OR IGNORE INTO resumptions VALUES(?,?,?)", (owner, version, reference)
            )

    def admit_windows(self, owner: str, version: str | None, count: int) -> None:
        """Preflight only. Actual concurrent execution still reserves per attempt."""
        finite_integer(count, "window count")
        if not owner or owner == "*":
            raise BudgetDenied("An owner identifier is required")
        if version is None:
            if count > 30:
                raise BudgetDenied("Unconfirmed primary cap is 30; actual N exceeds it")
            return
        with self._connect() as conn:
            for scope in ("*", owner):
                b = conn.execute(
                    "SELECT * FROM budgets WHERE scope=? AND version=?", (scope, version)
                ).fetchone()
                if b is None or not b["active"] or count > b["primary_cap"] - b["used_primary"]:
                    raise BudgetDenied("Actual N exceeds remaining approved primary budget")

    def admit_cost(self, owner, version, calls, money_units, currency_unit):
        """Conservative whole-job preflight, followed by atomic per-attempt reserve."""
        finite_integer(calls, "calls")
        finite_integer(money_units, "money_units")
        if not owner or owner == "*":
            raise BudgetDenied("An owner identifier is required")
        with self._connect() as conn:
            for scope in ("*", owner):
                row = conn.execute(
                    "SELECT * FROM budgets WHERE scope=? AND version=?", (scope, version)
                ).fetchone()
                if (
                    not row
                    or not row["active"]
                    or row["currency"] != currency_unit
                    or calls > row["call_cap"] - row["used_calls"]
                    or money_units > row["money_cap"] - row["used_money"]
                ):
                    raise BudgetDenied("Insufficient approved whole-job budget")

    def reserve(
        self,
        *,
        attempt_id: str,
        owner: str,
        version: str,
        payload_hash: str,
        kind: str,
        money_units: int,
        currency_unit: str,
    ) -> str:
        finite_integer(money_units, "money_units", minimum=1)
        if owner == "*" or not all((attempt_id, owner, version, payload_hash)):
            raise BudgetDenied("Attempt and owner identifiers are required")
        if kind not in {"coarse", "zoom", "asr", "embed", "rerank", "text", "allocation"}:
            raise BudgetDenied("Unknown model/resource attempt kind")
        primary = int(kind == "coarse")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if previous:
                same = (
                    previous["owner"],
                    previous["version"],
                    previous["payload_hash"],
                    previous["kind"],
                    previous["money"],
                    previous["currency"],
                )
                if same != (owner, version, payload_hash, kind, money_units, currency_unit):
                    raise BudgetDenied("Attempt ID collision with different payload/owner")
                return previous["state"]
            for scope in ("*", owner):
                updated = conn.execute(
                    "UPDATE budgets SET used_primary=used_primary+?,used_calls=used_calls+1,"
                    "used_money=used_money+? WHERE scope=? AND version=? AND active=1 "
                    "AND currency=? AND used_primary+?<=primary_cap AND used_calls+1<=call_cap "
                    "AND used_money+?<=money_cap",
                    (primary, money_units, scope, version, currency_unit, primary, money_units),
                )
                if updated.rowcount != 1:
                    raise BudgetDenied("Unknown, revoked, insufficient or incompatible budget")
            conn.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?, 'reserved')",
                (attempt_id, owner, version, payload_hash, kind, money_units, currency_unit),
            )
        return "reserved"

    def transition(self, attempt_id: str, state: str) -> None:
        allowed = {
            "dispatched": ("reserved",),
            "completed": ("dispatched",),
            "unknown": ("reserved", "dispatched"),
        }
        if state not in allowed:
            raise BudgetDenied("Unsupported attempt transition")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["state"] not in allowed[state]:
                raise BudgetDenied("Unknown or non-replayable attempt state")
            if state == "dispatched":
                for scope in ("*", attempt["owner"]):
                    b = conn.execute(
                        "SELECT * FROM budgets WHERE scope=? AND version=?",
                        (scope, attempt["version"]),
                    ).fetchone()
                    if not b or not b["active"] or b["used_money"] > b["money_cap"]:
                        raise BudgetDenied("Approval revoked or lowered before dispatch")
            conn.execute("UPDATE attempts SET state=? WHERE id=?", (state, attempt_id))

    def read_attempt(self, attempt_id: str, owner: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE id=? AND owner=?", (attempt_id, owner)
            ).fetchone()
            return dict(row) if row else None
