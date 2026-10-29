"""Optional loopback workspace adapter to the existing ledger over operator SSH.

No approvals, resets, refunds, provider calls or transport retries. The remote
helper only exposes existing atomic ledger operations, using a private binding.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from frameseek.attempts import AttemptLedger, BudgetDenied

METHODS = {"admit_windows", "admit_cost", "reserve", "transition", "read_attempt", "snapshot"}


def dispatch(ledger, binding, request):
    method, args = request["method"], dict(request.get("args", {}))
    if method not in METHODS:
        raise BudgetDenied("Unsupported shared ledger operation")
    owner, version = binding["remote_owner"], binding["version"]
    if "owner" in args and args["owner"] != binding["local_owner"]:
        raise BudgetDenied("Shared ledger owner mismatch")
    if "owner" in args:
        args["owner"] = owner
    if "version" in args and args["version"] != version:
        raise BudgetDenied("Shared ledger version mismatch")
    with ledger._connect() as conn:
        budgets = conn.execute(
            "SELECT * FROM budgets WHERE version=? AND scope IN (?, '*')", (version, owner)
        ).fetchall()
        if len(budgets) != 2 or any(
            b["call_cap"] != binding["call_cap"]
            or b["money_cap"] != binding["money_cap"]
            or b["currency"] != "USD_MICRO"
            for b in budgets
        ):
            raise BudgetDenied("Shared approved envelope changed")
    if method == "snapshot":
        with ledger._connect() as conn:
            result = {}
            for table, where, params in (
                ("budgets", "version=? AND scope IN (?, '*')", (version, owner)),
                ("attempts", "owner=?", (owner,)),
            ):
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                rows = [
                    list(row)
                    for row in conn.execute(f"SELECT * FROM {table} WHERE {where}", params)
                ]
                key = "scope" if table == "budgets" else "owner"
                for row in rows:
                    if row[columns.index(key)] == owner:
                        row[columns.index(key)] = binding["local_owner"]
                result[table] = {"columns": columns, "rows": rows}
            return result
    if method == "transition":
        attempt = ledger.read_attempt(args["attempt_id"], owner)
        if not attempt or attempt["version"] != version:
            raise BudgetDenied("Shared attempt not owned by binding")
    result = getattr(ledger, method)(**args)
    if method == "read_attempt" and result:
        result["owner"] = binding["local_owner"]
    return result


class SharedLedger:
    allow_repairs = False
    direct_observations = True

    def __init__(self, target, local_owner, version):
        if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", target):
            raise BudgetDenied("Invalid shared SSH target")
        self.target, self.local_owner, self.version = target, local_owner, version

    def _rpc(self, method, **args):
        if "owner" in args and args["owner"] != self.local_owner:
            raise BudgetDenied("Shared ledger owner mismatch")
        if "version" in args and args["version"] != self.version:
            raise BudgetDenied("Shared ledger version mismatch")
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    self.target,
                    "sudo /opt/frameseek/.venv/bin/python /opt/frameseek/shared-ledger-rpc.py",
                ],
                input=json.dumps({"method": method, "args": args}),
                text=True,
                capture_output=True,
                timeout=20,
                check=True,
            )
            response = json.loads(result.stdout)
            if not response.get("ok"):
                raise BudgetDenied("Shared ledger rejected operation")
            return response["result"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            raise BudgetDenied("Shared ledger unavailable; no automatic retry") from error

    @contextmanager
    def _connect(self):
        # Readonly reporting uses an ephemeral snapshot, never a second writable ledger.
        data = self._rpc("snapshot")
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            for table, contents in data.items():
                if table not in {"budgets", "attempts"}:
                    raise BudgetDenied("Invalid shared snapshot")
                columns = contents["columns"]
                if any(not re.fullmatch(r"[a-z_]+", c) for c in columns):
                    raise BudgetDenied("Invalid shared snapshot columns")
                conn.execute(f"CREATE TABLE {table} ({','.join(columns)})")
                conn.executemany(
                    f"INSERT INTO {table} VALUES({','.join('?' for _ in columns)})",
                    contents["rows"],
                )
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()

    def admit_cost(self, owner, version, calls, money_units, currency_unit):
        return self._rpc(
            "admit_cost",
            owner=owner,
            version=version,
            calls=calls,
            money_units=money_units,
            currency_unit=currency_unit,
        )

    def admit_windows(self, owner, version, count):
        return self._rpc("admit_windows", owner=owner, version=version, count=count)

    def reserve(self, **args):
        return self._rpc("reserve", **args)

    def transition(self, attempt_id, state):
        return self._rpc("transition", attempt_id=attempt_id, state=state)

    def read_attempt(self, attempt_id, owner):
        return self._rpc("read_attempt", attempt_id=attempt_id, owner=owner)


def main():
    binding = json.loads(Path("/etc/frameseek/local-ledger-bridge.json").read_text())
    root = Path("/srv/frameseek/workspace")
    with sqlite3.connect(f"file:{root / 'workspace.sqlite3'}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT expires FROM sessions WHERE id=?", (binding["remote_owner"],)
        ).fetchone()
        if not row or row[0] <= time.time():
            raise BudgetDenied("Shared ledger owner expired")
    request = json.loads(sys.stdin.read(65536))
    try:
        result = dispatch(AttemptLedger(root / "attempts.sqlite3"), binding, request)
        print(json.dumps({"ok": True, "result": result}))
    except (BudgetDenied, KeyError, TypeError, ValueError):
        print(json.dumps({"ok": False}))


def configured_ledger(path, runtime):
    target = os.environ.get("FS_SHARED_LEDGER_SSH")
    if not target:
        ledger = AttemptLedger(path)
        if os.environ.get("FS_DIRECT_OBSERVATIONS") == "1":
            if not runtime or runtime.provider != "tokenfactory":
                raise BudgetDenied("Direct observations require Token Factory")
            ledger.allow_repairs = False
            ledger.direct_observations = True
        if os.environ.get("FS_VISUAL_SEARCH") == "1":
            if not runtime or runtime.provider != "tokenfactory":
                raise BudgetDenied("Visual search requires Token Factory")
            ledger.allow_repairs = False
            ledger.direct_observations = True
            ledger.visual_search = True
        return ledger
    if not runtime or os.environ.get("FS_PUBLIC_ORIGIN"):
        raise BudgetDenied("Shared ledger is restricted to the private local workspace")
    if os.environ.get("FS_VISUAL_SEARCH") == "1" and runtime.provider != "tokenfactory":
        raise BudgetDenied("Visual search requires Token Factory")
    ledger = SharedLedger(
        target, os.environ["FS_SHARED_LEDGER_LOCAL_OWNER"], runtime.budget_version
    )
    ledger.visual_search = os.environ.get("FS_VISUAL_SEARCH") == "1"
    return ledger


if __name__ == "__main__":
    main()
