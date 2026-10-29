"""Local scoreboard-assisted shot verification; never counts OCR deltas alone."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from frameseek.occurrences import fingerprint
from frameseek.schema import Answer, hms
from frameseek.score_transitions import ScoreReading, propose_score_changes


def score_target(question):
    match = re.search(r"(?:(\d+)\s*分球|([二三两])分球|\b(three|two)[ -]point)", question, re.I)
    if not match or not re.search(r"多少|几个|次数|总数|how many|count", question, re.I):
        return None
    return (
        int(match[1])
        if match[1]
        else {"二": 2, "两": 2, "三": 3, "two": 2, "three": 3}[
            next(g for g in match.groups()[1:] if g).lower()
        ]
    )


def configured():
    binary = Path(os.environ.get("FS_SCORE_OCR_BIN", ""))
    return (
        binary if binary.is_absolute() and binary.is_file() and os.access(binary, os.X_OK) else None
    )


def reading(frame, rows):
    confident = [r for r in rows if r.get("confidence", 0) >= 0.9]
    clocks = []
    for row in confident:
        text = row["text"].strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if match and int(match[2]) < 60:
            clocks.append((row, float(int(match[1]) * 60 + int(match[2]))))
        elif re.fullmatch(r"\d{1,2}\.\d{1,2}", text) and float(text) < 60:
            clocks.append((row, float(text)))
    for clock, remaining in clocks:
        aligned = [
            row
            for row in confident
            if abs(row["y"] + row["h"] / 2 - clock["y"] - clock["h"] / 2)
            <= max(row["h"], clock["h"]) * 0.5
        ]
        aligned.sort(key=lambda row: row["x"])
        scores, periods = {}, []
        for i, row in enumerate(aligned):
            text = row["text"].strip()
            pair = re.fullmatch(r"([A-Z]{2,5})\s*(\d{1,3})", text)
            if pair:
                label, value = pair[1], int(pair[2])
            elif (
                re.fullmatch(r"[A-Z]{2,5}", text)
                and i + 1 < len(aligned)
                and re.fullmatch(r"\d{1,3}", aligned[i + 1]["text"].strip())
                and 0 <= aligned[i + 1]["x"] - row["x"] - row["w"] < 0.07
            ):
                label, value = text, int(aligned[i + 1]["text"])
            else:
                label = None
            if label:
                if label in scores:
                    return None
                scores[label] = value
            if re.fullmatch(r"[1-4](?:st|nd|rd|th)|(?:\d+)?OT", text, re.I):
                periods.append(text.lower())
        if len(scores) == 2 and len(periods) == 1:
            return ScoreReading(
                t=float(frame["t"]),
                segment="clock-continuity",
                period=periods[0],
                clock_remaining=remaining,
                scores=scores,
                trusted=True,
            )
    return None


def candidates(scanned, delta):
    readings = []
    segment = 0
    previous = None
    for item in scanned:
        current = reading(item["frame"], item["rows"])
        if current is None:
            # A bounded OCR gap can propose a clip for pixel verification, never
            # prove an event. propose_score_changes still rejects >2s gaps.
            continue
        if previous and (
            previous.clock_remaining - current.clock_remaining > current.t - previous.t + 2
            or current.clock_remaining > previous.clock_remaining
        ):
            segment += 1
        current = current.model_copy(update={"segment": str(segment)})
        readings.append(current)
        previous = current
    return propose_score_changes(readings, delta)


def scan(directory, cache, frames, progress, active, write_private):
    binary = configured()
    revision = fingerprint(
        {"worker": hashlib.sha256(binary.read_bytes()).hexdigest(), "format": "score-ocr-v1"}
    )
    scanned = []
    for offset in range(0, len(frames), 8):
        if not active():
            raise ValueError("query_cancelled")
        batch = frames[offset : offset + 8]
        key = fingerprint({"revision": revision, "frames": batch})[:24]
        receipt = cache / f"ocr-{key}.json"
        if receipt.is_file():
            result = json.loads(receipt.read_text())
        else:
            files = [directory / "frames" / f["file"] for f in batch]
            if any(
                f.is_symlink() or f.resolve().parent != (directory / "frames").resolve()
                for f in files
            ):
                raise ValueError("ocr_frame_path_invalid")
            env = {
                k: v
                for k, v in os.environ.items()
                if not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            }
            run = subprocess.run(
                [str(binary)],
                input=json.dumps([str(f) for f in files]) + "\n",
                text=True,
                capture_output=True,
                timeout=40,
                check=True,
                env=env,
            )
            if len(run.stdout) > 2 * 1024 * 1024:
                raise ValueError("ocr_output_limit")
            result = json.loads(run.stdout)
            if not isinstance(result, list) or len(result) != len(batch):
                raise ValueError("ocr_output_invalid")
            write_private(receipt, result)
        scanned.extend(
            {"frame": f, "rows": row["rows"]} for f, row in zip(batch, result, strict=True)
        )
        progress(f"正在读取记分牌 {min(offset + 8, len(frames))}/{len(frames)} 帧…")
    return scanned


def answer_scores(
    directory, source, question, rows, cache, client, progress, active, write_private, json_response
):
    delta = score_target(question)
    frames = sorted(
        {f["frame_id"]: f for row in rows for f in row["frames"]}.values(), key=lambda f: f["t"]
    )
    from frameseek.agent import explicit_time_window

    metadata = json.loads(source["metadata"])
    scope = explicit_time_window(question, metadata["duration"], max_seconds=metadata["duration"])
    if scope:
        frames = [f for f in frames if scope["start"] <= f["t"] < scope["end"]]
    scanned = scan(directory, cache, frames, progress, active, write_private)
    proposed = candidates(scanned, delta)
    unique = {}
    for proposal in proposed:
        key = fingerprint(
            {"side": proposal["side"], "before": proposal["before"], "after": proposal["after"]}
        )
        unique.setdefault(key, proposal)
    proposed = list(unique.values())
    score_records = []
    for proposal in proposed[:100]:
        pair = [f for f in frames if f["t"] in [proposal["start"], proposal["end"]]]
        start, end = pair[0]["t"], pair[-1]["t"] + 0.001
        score_records.append(
            {
                "video_id": source["id"],
                "start": start,
                "end": end,
                "start_hms": hms(start),
                "end_hms": hms(end),
                "action": f"记分牌增加{delta}分",
                "summary": (
                    f"{proposal['side']} 比分 {proposal['before'][proposal['side']]}"
                    f"→{proposal['after'][proposal['side']]}；"
                    "这是记分牌读数，投篮动作尚未确认。"
                ),
                "support_level": "low",
                "evidence": [
                    {
                        "t": f["t"],
                        "t_hms": hms(f["t"]),
                        "frame_id": f["frame_id"],
                        "uri": f["uri"],
                        "caption": "记分牌变化的前后原始帧；不冒充动作识别结论。",
                        "asr": None,
                    }
                    for f in pair
                ],
            }
        )
    verified = []
    score_updates = []
    uncertain = []
    seen = set()
    if len(proposed) > 100:
        uncertain.append(f"候选超过单轮100事件复核上限，另有{len(proposed) - 100}处未复核。")
    for n, p in enumerate(proposed[:100]):
        if not active():
            raise ValueError("query_cancelled")
        selected = [f for f in frames if p["end"] - 6 <= f["t"] <= p["end"] + 1][:8]
        if len(selected) < 4:
            uncertain.append("得分变化附近画面不足，未计数。")
            continue
        prompt = (
            f"复核这组连续篮球画面中是否存在一次{delta}分投篮命中。图片按时间正序排列。"
            "下方OCR是候选线索，不是投篮结论。你负责检查画面是否有投篮动作和随后回合变化，"
            "排除连续罚球合计、回放、比分更正或未见动作的剪辑。"
            "若明确看见一次投篮出手、之后对手发底线球或攻守转换，结合稳定的同队加分，"
            "可以综合确认命中；不要求某一张抽帧必须刚好拍到球穿过篮网。"
            "若看不到投篮动作，或分不清是否回放，confirmed=false。不要猜人物姓名。"
            "support_frames引用直接支持动作与回合变化的0起始图片编号；不要输出绝对时间。"
            "reason简短说明画面依据，不要重新猜测OCR比分。只返回JSON。\n"
            + json.dumps(
                {
                    "ocr_candidate": {
                        "side": p["side"],
                        "before": p["before"],
                        "after": p["after"],
                        "delta": delta,
                    },
                    "image_order": list(range(len(selected))),
                },
                ensure_ascii=False,
            )
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["confirmed", "is_replay", "support_frames", "reason"],
            "properties": {
                "confirmed": {"type": "boolean"},
                "is_replay": {"type": "boolean"},
                "support_frames": {
                    "type": "array",
                    "items": {"enum": list(range(len(selected)))},
                    "minItems": 0,
                    "maxItems": 8,
                },
                "reason": {"type": "string", "maxLength": 500},
            },
        }
        key = fingerprint(
            {
                "format": "score-shot-verification-v2",
                "question": question,
                "frames": selected,
                "prompt": prompt,
            }
        )[:24]
        receipt = cache / f"shot-{key}.raw.json"
        started = cache / f"shot-{key}.started"
        progress(f"正在复核得分事件 {n + 1}/{len(proposed)}（比分更新 {hms(p['end'])}）…")
        try:
            if receipt.is_file():
                raw = json.loads(receipt.read_text())
            else:
                if started.exists():
                    raise ValueError("shot_verification_interrupted_unknown")
                write_private(started, {"key": key})
                raw = client.infer(
                    prompt,
                    [directory / "frames" / f["file"] for f in selected],
                    max_tokens=2048,
                    schema=schema,
                )
                write_private(receipt, raw)
                started.unlink(missing_ok=True)
            result = json_response(raw["text"])
            if (
                type(result.get("confirmed")) is not bool
                or type(result.get("is_replay")) is not bool
            ):
                raise ValueError("shot_verification_invalid")
            indices = result.get("support_frames")
            if not isinstance(indices, list) or any(
                type(i) is not int or not 0 <= i < len(selected) for i in indices
            ):
                raise ValueError("shot_reference_invalid")
            canonical = fingerprint({"side": p["side"], "before": p["before"], "after": p["after"]})
            if result["is_replay"] or canonical in seen:
                continue
            if not result["confirmed"] or not indices:
                uncertain.append(
                    f"{hms(p['start'])}–{hms(p['end'])} 得分变化未能确认单次投篮命中。"
                )
                continue
            seen.add(canonical)
            score_updates.append(p["end"])
            evidence = {
                f["frame_id"]: f
                for f in [selected[i] for i in indices]
                + [f for f in frames if f["t"] in [p["start"], p["end"]]]
            }
            event_frames = sorted(evidence.values(), key=lambda f: f["t"])
            start, end = event_frames[0]["t"], event_frames[-1]["t"] + 0.001
            verified.append(
                {
                    "video_id": source["id"],
                    "start": start,
                    "end": end,
                    "start_hms": hms(start),
                    "end_hms": hms(end),
                    "action": f"{delta}分投篮命中复核",
                    "summary": (
                        f"{p['side']} 比分 {p['before'][p['side']]}→{p['after'][p['side']]}；"
                        f"比分在 {hms(p['end'])} 更新，投篮发生在此前画面。"
                    ),
                    "support_level": "low",
                    "evidence": [
                        {
                            "t": f["t"],
                            "t_hms": hms(f["t"]),
                            "frame_id": f["frame_id"],
                            "uri": f["uri"],
                            "caption": "投篮复核与前后比分证据；时间取自原视频帧。",
                            "asr": None,
                        }
                        for f in event_frames
                    ],
                }
            )
        except ValueError as error:
            uncertain.append("复核未完成，保留已确认事件：" + str(error)[:100])
            break
    missing = sum(reading(x["frame"], x["rows"]) is None for x in scanned)
    from collections import Counter

    sides = Counter(p["side"] for p in proposed)
    text = (
        f"按记分牌变化统计，已检查画面中有 {len(proposed)} 次单队加{delta}分记录"
        + (
            "（" + "，".join(f"{side} {count}次" for side, count in sides.items()) + "）。"
            if proposed
            else "。"
        )
        if proposed
        else "当前未读到满足稳定性要求的得分变化，不能据此说视频里没有。"
    )
    if proposed:
        text += "比分更新时间：" + "、".join(hms(p["end"]) for p in proposed[:100]) + "。"
        text += (
            f"可作为{delta}分命中的记分牌统计估计；其中 {len(verified)} 次通过动作模型复核，"
            f"{len(proposed) - len(verified)} 次尚未通过。请回看每次前后比分。"
        )
    text += f"{missing}/{len(frames)} 张抽帧未能可靠读取记分牌；抽帧可能漏检，不保证全片无遗漏。"
    events = verified if len(verified) == len(score_records) else score_records
    return Answer.model_validate(
        {
            "schema_version": "0.1",
            "status": "partial" if events else "no_result",
            "timebase": "video_absolute_seconds",
            "answer": text,
            "events": events,
            "unresolved": (
                uncertain[:98]
                + [
                    "计数为稳定记分牌加分记录，未通过动作复核的记录不是已验证的投篮命中；不代表完整比赛总数。"
                ]
            )
            if events
            else [],
            "followups": [],
            "errors": [],
            "trace": {
                "visible": {
                    "channels": [],
                    "segments_reviewed": len(frames),
                    "evidence_segments": len(
                        {f["frame_id"] for e in events for f in e["evidence"]}
                    ),
                },
                "debug": None,
            },
        }
    ).model_dump(mode="json")
