"""Explicit local operator interface. The web app never invokes approval installation."""

import argparse
import json
import os
from pathlib import Path

from frameseek.attempts import ApprovedBudget, AttemptLedger


def main():
    parser = argparse.ArgumentParser(description="Local approved-budget ledger administration")
    parser.add_argument("action", choices=["show", "install", "revoke", "settle", "resume"])
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("FS_DATA_DIR", ".frameseek-data"))
    )
    parser.add_argument("--file", type=Path)
    parser.add_argument("--confirm-approved-reference")
    parser.add_argument("--scope")
    parser.add_argument("--version")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = AttemptLedger(args.data_dir / "attempts.sqlite3")
    if args.action == "install":
        if not args.file or not args.confirm_approved_reference:
            parser.error(
                "install requires the finite approved record and its human approval reference"
            )
        records = json.loads(args.file.read_text())
        budgets = [ApprovedBudget(**record) for record in records]
        if not budgets or any(
            b.approval_reference != args.confirm_approved_reference for b in budgets
        ):
            parser.error(
                "approval reference must match every record; "
                "ordinary runtime config is not approval"
            )
        # Validate all inputs before writing. Operator must create a fresh version,
        # covering both '*' deployment and one explicitly authorized session owner.
        if {b.scope for b in budgets} != {"*", args.scope} or not args.scope or args.scope == "*":
            parser.error("provide --scope owner_id; records must cover deployment and that owner")
        if len(budgets) != 2 or len({b.version for b in budgets}) != 1:
            parser.error("exactly two matching new-version approval records are required")
        with ledger._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for b in budgets:
                c.execute(
                    "INSERT INTO budgets(scope,version,reference,primary_cap,"
                    "call_cap,money_cap,currency) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        b.scope,
                        b.version,
                        b.approval_reference,
                        b.primary_calls,
                        b.model_calls,
                        b.money_units,
                        b.currency_unit,
                    ),
                )
        print("Approved records installed. No model request or GPU resource was started.")
    elif args.action == "settle":
        if not args.file or not args.confirm_approved_reference:
            parser.error("settle requires an evidence file and its confirmed reference")
        records = json.loads(args.file.read_text())
        if (
            not isinstance(records, list)
            or not records
            or any(r.get("reference") != args.confirm_approved_reference for r in records)
        ):
            parser.error("every settlement must name the confirmed evidence reference")
        for record in records:
            ledger.settle_completed(**record)
        print("Completed reservations reconciled. Calls and unknown reservations retained.")
    elif args.action == "resume":
        if not args.scope or not args.version or not args.confirm_approved_reference:
            parser.error(
                "resume requires owner, existing version and human authorization reference"
            )
        ledger.resume_existing(args.scope, args.version, args.confirm_approved_reference)
        print("Existing approval resumed. Original caps and consumed counters retained.")
    elif args.action == "revoke":
        if not args.scope or not args.version:
            parser.error("revoke requires --scope and --version")
        ledger.restrict(args.scope, args.version)
        print("Approval revoked; future dispatches stop. Existing costs are retained.")
    else:
        with ledger._connect() as c:
            rows = c.execute(
                "SELECT scope,version,active,primary_cap,call_cap,money_cap,currency,"
                "used_primary,used_calls,used_money FROM budgets"
            ).fetchall()
        print(json.dumps([dict(row) for row in rows], indent=2))


if __name__ == "__main__":
    main()
