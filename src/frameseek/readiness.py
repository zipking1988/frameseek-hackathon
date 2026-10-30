"""Read-only local dispatch prerequisites. No endpoint calls or approval installation."""

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from frameseek.agent import AgentError, load_runtime
from frameseek.config import ConfigurationError, load_settings


def budget_ready(connection, owner, runtime):
    if not owner or not runtime:
        return False
    for scope in ("*", owner):
        row = connection.execute(
            "SELECT active,currency,call_cap,used_calls,money_cap,used_money "
            "FROM budgets WHERE scope=? AND version=?",
            (scope, runtime.budget_version),
        ).fetchone()
        if not row:
            return False
        active, currency, calls, used_calls, money, used_money = row
        if (
            not active
            or currency != runtime.currency_unit
            or calls <= used_calls
            or money <= used_money
        ):
            return False
    return True


def readiness_report(settings, runtime, approved=False):
    checks = []
    provider = runtime.provider if runtime else "nvidia_nebius"
    gemini = provider == "gemini"

    def check(id, label, passed, detail):
        checks.append(
            {
                "id": id,
                "label": label,
                "state": "available" if passed else "blocked",
                "detail": detail,
            }
        )

    check(
        "mode",
        "Live analysis mode",
        settings.mode == "real",
        "Fixture mode provides preset demonstrations. Live mode "
        "requires trusted configuration; this page cannot approve "
        "spending.",
    )
    check(
        "media_tools",
        "Video tools",
        all(shutil.which(t) for t in ("ffmpeg", "ffprobe")),
        "Upload preflight and evidence extraction require FFmpeg and "
        "ffprobe; deployment isolation requires separate "
        "verification.",
    )
    blockers = runtime.blockers() if runtime else ["model_endpoints_missing"]
    roles = []
    for role, label in (
        ("vision", "Visual review"),
        ("embed", "Vector retrieval"),
        ("rerank", "Candidate reranking"),
        ("text", "Text synthesis"),
        ("asr", "Audio transcription"),
    ):
        endpoint = getattr(runtime, role, None) if runtime else None
        key_present = bool(endpoint and (not endpoint.key_env or os.environ.get(endpoint.key_env)))
        roles.append(
            {
                "role": role,
                "label": label,
                "optional": role == "asr",
                "configured": bool(endpoint),
                "key_present": key_present,
                "model": endpoint.model if endpoint else None,
                "revision": endpoint.revision if endpoint else None,
            }
        )
    required = all(r["configured"] and r["key_present"] for r in roles if not r["optional"])
    check(
        "models",
        "Model configuration and credentials",
        required,
        "This check only reads configuration and does not contact "
        "models. Audio transcription is optional and unavailable if "
        "not configured.",
    )
    check(
        "endpoints",
        "Endpoints and media boundaries",
        bool(runtime)
        and not any(
            b in blockers
            for b in (
                "invalid_model_endpoint",
                "media_endpoint_not_self_hosted",
                "remote_endpoint_requires_tls",
                "model_revision_missing",
                "gemini_endpoint_not_allowed",
                "invalid_gemini_model",
                "google_media_not_enabled",
                "gemini_asr_not_supported",
                "tokenfactory_endpoint_not_allowed",
                "tokenfactory_model_not_allowed",
                "tokenfactory_media_not_enabled",
                "tokenfactory_asr_not_supported",
            )
        ),
        (
            "The Gemini route sends locally decoded JPEG frames to the "
            "Google Gemini API only when explicitly enabled in trusted "
            "configuration."
            " Text and retrieval stages do not send media; audio transcription is not supported."
            if gemini
            else "The Token Factory route sends locally extracted JPEGs only "
            "to the fixed official API; "
            "retrieval, reranking and answer stages send text only. "
            "Calls require budget approval; audio transcription is not "
            "supported."
            if provider == "tokenfactory"
            else "Frames and audio are sent only to allowlisted self-hosted "
            "services over HTTPS when remote; text services do not "
            "receive media."
        ),
    )
    calibrated = bool(
        runtime
        and runtime.valid_until > time.time()
        and runtime.calibration_reference
        and runtime.resource_cutoff_reference
    )
    check(
        "calibration",
        "Cost calibration and resource cutoff",
        calibrated,
        "This checks calibration references and expiry, not provider billing cutoff or acceptance.",
    )
    check(
        "budget",
        "Session and deployment budgets",
        approved,
        "Both matching budget approvals must be active. Each "
        "analysis is admitted against its actual windows and costs.",
    )
    return {
        "mode": settings.mode,
        "provider": provider,
        "checks": checks,
        "models": roles,
        "runtime_blockers": blockers,
        "dispatch_prerequisites_met": not blockers
        and all(c["state"] == "available" for c in checks),
        "live_verified": False,
        "model_requests_sent_by_check": 0,
        "note": "Local admission check only; it does not verify model "
        "connectivity, quality, performance, or deployment readiness.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner", help="Existing local session owner ID; optional, not an approval"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("FS_DATA_DIR", ".frameseek-data"))
    )
    args = parser.parse_args()
    try:
        settings, runtime = load_settings(), load_runtime()
        approved = False
        path = args.data_dir / "attempts.sqlite3"
        if path.is_file() and args.owner and runtime:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                approved = budget_ready(connection, args.owner, runtime)
            finally:
                connection.close()
        report = readiness_report(settings, runtime, approved)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["dispatch_prerequisites_met"]:
            raise SystemExit(2)
    except (AgentError, ConfigurationError, OSError, sqlite3.Error):
        parser.exit(2, "Local configuration or budget ledger unavailable; no model request sent.\n")


if __name__ == "__main__":
    main()
