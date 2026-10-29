"""Four deterministic schema fixtures. No media inference or paid calls."""

import json

from frameseek.schema import Answer


def examples() -> dict[str, Answer]:
    event = {
        "video_id": "fixture_video",
        "start": 2.0,
        "end": 4.0,
        "start_hms": "00:00:02",
        "end_hms": "00:00:04",
        "action": "示例动作",
        "summary": "仅供合同校验的固定事件，不是模型识别结果。",
        "support_level": "medium",
        "evidence": [
            {
                "t": 3.0,
                "t_hms": "00:00:03",
                "frame_id": "fixture_video_f3",
                "uri": "/media/fixture_video/frames/3.jpg",
                "caption": "固定示例证据",
                "asr": None,
            }
        ],
    }
    base = {
        "schema_version": "0.1",
        "timebase": "video_absolute_seconds",
        "answer": "MODE=fixture：固定合同样例，不代表模型效果。",
        "events": [],
        "unresolved": [],
        "followups": [],
        "errors": [],
        "trace": {
            "visible": {
                "channels": ["visual_vector"],
                "segments_reviewed": 1,
                "evidence_segments": 0,
            },
            "debug": None,
        },
    }
    result = {}
    for status in ("ok", "partial", "no_result", "error"):
        item = {**base, "status": status}
        if status in {"ok", "partial"}:
            item["events"] = [event]
            item["trace"] = {
                "visible": {
                    "channels": ["visual_vector"],
                    "segments_reviewed": 1,
                    "evidence_segments": 1,
                },
                "debug": None,
            }
        if status == "partial":
            item["unresolved"] = ["固定样例的后续范围尚未检查"]
        if status == "error":
            item["errors"] = [
                {
                    "code": "fixture_failure",
                    "stage": "fixture",
                    "message": "固定失败样例",
                    "cause": "unknown",
                    "retryable": False,
                    "suggested_action": "此为合同校验样例，无需发起真实调用。",
                    "docs_url": "/docs/errors",
                    "request_id": "fixture_error",
                }
            ]
        result[status] = Answer.model_validate(item)
    return result


def main() -> None:
    print(
        json.dumps(
            {
                "mode": "fixture",
                "model_requests_sent": 0,
                "real_quality_evaluation": False,
                "examples": {k: v.model_dump() for k, v in examples().items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
