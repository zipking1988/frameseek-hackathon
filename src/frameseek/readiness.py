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
        "真实分析模式",
        settings.mode == "real",
        "fixture 只提供预设演示；切换真实模式需要本机配置，不能由此页面批准费用。",
    )
    check(
        "media_tools",
        "本机视频工具",
        all(shutil.which(t) for t in ("ffmpeg", "ffprobe")),
        "上传预检和证据抽取需要 FFmpeg / ffprobe；部署隔离仍需单独验证。",
    )
    blockers = runtime.blockers() if runtime else ["model_endpoints_missing"]
    roles = []
    for role, label in (
        ("vision", "视觉回看"),
        ("embed", "向量检索"),
        ("rerank", "候选重排"),
        ("text", "文本整理"),
        ("asr", "音轨转写"),
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
        "模型配置与凭据",
        required,
        "此检查仅读取配置，不连接模型。ASR 可选；未配置时不宣称音轨理解能力。",
    )
    check(
        "endpoints",
        "端点与媒体边界",
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
            "Gemini 路线将本机解码的 JPEG 帧发往 Google Gemini API；需在私有配置中明确启用。"
            "文本和检索阶段不发送媒体；当前不支持音轨转写。"
            if gemini
            else "Token Factory 路线仅向固定官方 API 发送本机抽取的 JPEG；"
            "检索、重排和回答只发送文本。尚未批准预算时不会调用；不支持音轨转写。"
            if provider == "tokenfactory"
            else "帧和音频仅发送到已列出的自托管服务，远程服务需要 HTTPS，文本服务不接收媒体。"
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
        "费用校准与资源截止记录",
        calibrated,
        "只检查校准引用及有效期；不能证明供应商实际停止计费，也不是自动验收。",
    )
    check(
        "budget",
        "当前会话与部署预算",
        approved,
        "两级同版本预算必须已由可信本机操作者记录且有效。分析还需按实际窗口数和费用逐次准入。",
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
        "note": "本地准入检查，不是模型连通性、质量、性能或部署 GO 验收。",
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
