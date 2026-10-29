"""Read-only prerequisites; never starts GPU resources or contacts a model."""

import json
import shutil
import sys

from frameseek import __version__
from frameseek.config import ConfigurationError, Settings, load_settings

REAL_BLOCKERS = (
    "Live NVIDIA/Nebius endpoints and model/revision permissions have not been calibrated",
    "Finite numeric call/money/calibration budgets have not been confirmed",
    "GPU quota, billing rates and conservative resource release bounds are unverified",
    "Linux media isolation and deployed resource bounds still require verification",
)

GEMINI_BLOCKERS = (
    "Live Gemini model IDs/revisions, credentials and input/output bounds need verification",
    "Finite numeric call/money/calibration budgets have not been confirmed",
    "Gemini request cost bounds and provider billing controls are unverified",
    "Google Cloud durable storage and Linux media isolation remain deployment prerequisites",
)

TOKENFACTORY_BLOCKERS = (
    "Token Factory model revisions, credentials and bounded JPEG requests need live verification",
    "Finite numeric call/money/calibration budgets have not been confirmed",
    "Public API request cost bounds and provider billing controls are unverified",
    "Linux media isolation and deployed storage still require verification; no GPU rental needed",
)


def report(settings: Settings, provider: str = "nvidia_nebius") -> dict:
    tools = {name: shutil.which(name) is not None for name in ("ffmpeg", "ffprobe")}
    blockers = list(
        GEMINI_BLOCKERS
        if provider == "gemini"
        else TOKENFACTORY_BLOCKERS
        if provider == "tokenfactory"
        else REAL_BLOCKERS
    )
    if not all(tools.values()):
        blockers.append("ffmpeg and ffprobe are required for real media")
    return {
        "version": __version__,
        "mode": settings.mode,
        "provider": provider,
        "python": sys.version.split()[0],
        "local_tools": tools,
        "configuration": settings.model_dump(),
        "fixture_contract_checks_ready": True,
        "real_ready": False,
        "blockers": blockers,
        "model_requests_sent": 0,
        "gpu_resources_started": 0,
        "implemented": [
            "schema",
            "configuration",
            "window planning",
            "local budget ledger",
            "fixture workspace",
            "durable investigations and source library",
            "owned private media and bounded macOS decode",
            "NVIDIA/Nebius adapters and bounded agent integration",
            "Gemini REST adapter through the same approved attempt ledger",
            "idempotent chat and budget admission",
        ],
        "pending": [
            "live model calibration and paid resource cutoff proof",
            "live multimodal quality evaluation",
            "hour-long performance and concurrent workload validation",
            "real eval",
        ],
    }


def main() -> None:
    from frameseek.agent import AgentError, load_runtime

    try:
        settings = load_settings()
        runtime = load_runtime()
    except (ConfigurationError, AgentError) as exc:
        print(json.dumps({"code": "invalid_config", "message": str(exc)}))
        raise SystemExit(2) from None
    print(
        json.dumps(
            report(settings, runtime.provider if runtime else "nvidia_nebius"),
            ensure_ascii=False,
            indent=2,
        )
    )
    if settings.mode == "real":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
