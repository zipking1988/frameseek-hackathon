"""Owned callers bridge private reusable indexes to search and durable chat."""

import hashlib
import json
import os

from frameseek.media import MAX_UPLOAD
from frameseek.occurrences import fingerprint, load_index, search_occurrences
from frameseek.schema import Answer, hms
from frameseek.semantic_search import (
    available_conditions,
    caption_matches,
    interpret,
    remove_time_range,
    resolve_conditions,
)
from frameseek.visual_search import compile_index, requires_aggregation


def source_digest(directory):
    digest = hashlib.sha256()
    fd = os.open(directory / "original", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_UPLOAD:
            raise ValueError("source_limit")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def search_source(
    root,
    source,
    question,
    offset=0,
    limit=40,
    snapshot=None,
    conditions=None,
    knowledge=None,
    candidate_offset=0,
    candidate_time=None,
    include_candidates=False,
):
    aggregate = not conditions and requires_aggregation(question)
    video, generation = source["id"], source["generation"]
    directory = root / video
    metadata = json.loads(source["metadata"])
    from frameseek.agent import AgentError, explicit_time_window

    try:
        time_window = explicit_time_window(question, metadata["duration"], max_seconds=3600)
    except AgentError:
        return {
            "kind": "clarification_required",
            "message": "指定的视频时间范围无效。",
            "available_conditions": [],
            "model_requests_sent": 0,
        }
    query_for_terms = remove_time_range(question) if time_window else question
    artifact = directory / f"observations-g{generation}.json"
    if knowledge is not None or artifact.exists() or artifact.is_symlink():
        index = knowledge or load_index(
            artifact,
            video=video,
            generation=generation,
            source_fingerprint=source_digest(directory),
        )
        if index.duration != metadata["duration"]:
            raise ValueError("stale_index_duration")
        if aggregate:
            if time_window:
                return {
                    "kind": "clarification_required",
                    "message": "当前事件统计不支持指定时间范围；不会推断未观察的事件。",
                    "available_conditions": [],
                    "model_requests_sent": 0,
                }
            from frameseek.event_search import search_events

            result = search_events(index, question, offset, limit, snapshot)
            if result["kind"] == "clarification_required":
                return result
            result["provenance"] = index.extractor_revision
        else:
            predicates, problems = resolve_conditions(index, query_for_terms, conditions)
            if problems:
                return {
                    "kind": "clarification_required",
                    "message": " ".join(problems),
                    "available_conditions": available_conditions(index),
                    "model_requests_sent": 0,
                }
            result = search_occurrences(
                index,
                predicates,
                offset,
                limit,
                snapshot,
                time_window=time_window,
                candidate_offset=candidate_offset,
                candidate_time=candidate_time,
                include_candidates=include_candidates,
            )
            result.update(
                conditions=[c.model_dump() for c in predicates],
                available_conditions=available_conditions(index),
                structured_conditions_evaluated=True,
                verified_matches=False,
                explicit_conditions=bool(conditions),
                provenance=index.extractor_revision,
            )
        frames = [e for hit in result["occurrences"] for e in hit["evidence"]]
        frames.extend(result.get("uncertain_candidates", []))
    else:
        if aggregate:
            return {
                "kind": "clarification_required",
                "message": (
                    "这段视频尚无逐次事件观察；已有抽帧描述不能用来统计命中、总次数或排名。"
                    "原问题已保留，未调用模型。"
                ),
                "available_conditions": [],
                "model_requests_sent": 0,
            }
        if conditions:
            return {
                "kind": "clarification_required",
                "message": "当前只有画面描述，没有可用于同一物体筛选的观察索引。",
                "available_conditions": [],
                "model_requests_sent": 0,
            }
        terms, problems = interpret(query_for_terms)
        if problems or not terms:
            return {
                "kind": "clarification_required",
                "message": " ".join(problems) or "请写出要找的物体和可见条件。",
                "available_conditions": [],
                "model_requests_sent": 0,
            }
        if not source["manifest"]:
            raise ValueError("video_not_ready")
        index = compile_index(
            video, generation, metadata["duration"], json.loads(source["manifest"])
        )
        identity = fingerprint(
            {"index": index["fingerprint"], "question": question, "terms": terms}
        )
        if (offset and snapshot is None) or snapshot is not None and snapshot != identity:
            raise ValueError("index_snapshot_conflict")
        matches = [
            frame
            for frame in index["frames"]
            if (not time_window or time_window["start"] <= frame["t"] < time_window["end"])
            and caption_matches(frame["caption"], terms)
        ]
        end = offset + limit
        result = {
            "kind": "index_candidates",
            "frames": matches[offset:end],
            "total_candidates": len(matches),
            "indexed_frames": len(index["frames"]),
            "next_offset": end if end < len(matches) else None,
            "snapshot": identity,
            "verified_matches": False,
            "model_requests_sent": 0,
            "terms": terms,
            "available_conditions": [],
            "provenance": "legacy_saved_captions",
            "coverage": {
                "catalog_complete": False,
                "reviewed_frames": len(index["frames"]),
                "exhaustive_under_index": False,
                "all_source_frames_reviewed": False,
                "recognition_accuracy_verified": False,
            },
        }
        frames = result["frames"]
    for frame in frames:
        file = frame["uri"].rsplit("/", 1)[-1]
        path = directory / "frames" / file
        if (
            not path.is_file()
            or path.is_symlink()
            or path.resolve().parent != (directory / "frames").resolve()
        ):
            raise ValueError("indexed_evidence_missing")
    result.update(
        video_id=video, generation=generation, question=question, duration=metadata["duration"]
    )
    return result


def chat_answer(result):
    """Only supplied structured observations become evidence events, never captions."""
    events = []
    coverage = result["coverage"]
    if result["kind"] == "indexed_occurrences":
        for hit in result["occurrences"][:100]:
            # A fully catalogued frame can begin just before the requested
            # window but remain visible inside it. Keep its real presentation
            # timestamp within the answer event rather than inventing a frame.
            start = min(hit["start"], *(e["t"] for e in hit["evidence"]))
            end = hit["end"] if hit["end"] is not None else min(result["duration"], start + 0.001)
            if end <= start:
                continue
            events.append(
                {
                    "video_id": result["video_id"],
                    "start": start,
                    "end": end,
                    "start_hms": hms(start),
                    "end_hms": hms(end),
                    "action": result.get("aggregation", {}).get("action", "已有观察匹配"),
                    "summary": (
                        "已有事件观察的连续证据片段；同一已确认事件的回放只计一次。"
                        if result.get("aggregation")
                        else (
                            "索引记录的同一画面主体匹配全部检索条件；不推断跨镜头身份。"
                            + (
                                "首帧在所选时段开始前呈现，并持续到时段内。"
                                if start < hit["start"]
                                else ""
                            )
                        )
                    ),
                    "support_level": "low",
                    "evidence": [
                        {
                            "frame_id": e["frame_id"],
                            "t": e["t"],
                            "t_hms": hms(e["t"]),
                            "uri": e["uri"],
                            "caption": (
                                "已有事件观察的原视频画面；本轮未新增模型复核。"
                                if result.get("aggregation")
                                else "索引记录匹配条件；识别结果尚未验证，请回看原画面。"
                            ),
                            "asr": None,
                        }
                        for e in hit["evidence"]
                    ],
                }
            )
        total = result["total_occurrences"]
        text = (
            (
                f"在已有观察索引中找到{total}个匹配的抽样画面时点；这不是人数或事件次数。"
                if coverage.get("catalog_complete") is False and not result.get("aggregation")
                else f"在已有观察索引中找到{total}个出现区间/时点。"
            )
            if total
            else "已有观察索引中没有满足全部条件的匹配。"
        )
        if total and "有没有" in result.get("question", "") and not result.get("aggregation"):
            text = "索引中观察到有匹配。" + text
        if events and not result.get("aggregation"):
            # Quote actual frame timestamps, not the edges of an inferred span.
            times = list(dict.fromkeys(e["t_hms"] for event in events for e in event["evidence"]))
            if times:
                label = (
                    "部分引用画面时间"
                    if len(times) > 3 or result.get("next_offset")
                    else "引用画面时间"
                )
                text += f" {label}：{'、'.join(times[:3])}。"
        if not coverage["exhaustive_under_index"]:
            text += " 视频存在未检查或不确定范围，不能据此断言全部出现时间或全片不存在。"
        else:
            text += " 已遍历当前索引的全部源帧；识别准确性仍取决于原观察，未进行新的视觉复核。"
        if events and coverage.get("recognition_accuracy_verified") is not True:
            text += " 识别结果尚未验证，请回看引用画面。"
        if any(
            condition.get("value") in {"male", "female"}
            for condition in result.get("conditions", [])
        ):
            text += " 性别相关标签来自已有观察，未核实本人身份或性别认同。"
    else:
        total = result["total_candidates"]
        text = (
            f"已有描述中找到{total}张同时包含检索概念的候选画面。"
            "当前视频缺少完整物体观察索引，尚不能给出整段视频的全部出现区间。"
            "请打开索引时间线回看候选。"
        )
    if stats := result.get("aggregation"):
        scope = (
            "当前完整事件索引内"
            if stats["exact_under_index"]
            else "已有事件观察中（覆盖或去重不完整）"
        )
        text = (
            f"{scope}，{stats.get('label', stats['action'])}归并为{stats['unique_events']}组事件，"
            f"附{stats['evidence_clips']}段画面。"
        )
        if stats["repeated_clips"]:
            text += f"其中{stats['repeated_clips']}段由同一事件标识关联的重复/回放不重复计数。"
        if stats["mode"] == "ranking":
            text += (
                " 已有实体标识的统计："
                + (
                    "；".join(f"{row['entity']}：{row['events']}组" for row in stats["ranking"])
                    or "暂无"
                )
                + "。"
            )
            if not stats["ranking_complete"]:
                text += (
                    f"另有{stats['unknown_actors']}组未绑定实体，"
                    "或事件覆盖不完整，不能确认全片第一名。"
                )
        if not stats["exact_under_index"]:
            text += " 这些是当前观察的分组数，缺口两侧可能属于同一事件，不能当作整段视频总数。"
        text += " 事件语义和实体关联来自已有提取记录，本轮没有重新识别原视频。"
    if result.get("next_offset") is not None:
        text += " 当前回答只展示第一页；索引时间线可继续加载全部结果。"
    text += " 本次模型调用0次。"
    return Answer.model_validate(
        {
            "schema_version": "0.1",
            "status": "partial" if events else "no_result",
            "timebase": "video_absolute_seconds",
            "answer": text,
            "events": events,
            "unresolved": ["基于已有观察；请检查索引时间线中的覆盖信息与未检查范围。"]
            if events
            else [],
            "followups": [],
            "errors": [],
            "trace": {
                "visible": {
                    "channels": [],
                    "segments_reviewed": coverage["reviewed_frames"],
                    "evidence_segments": len(
                        {e["frame_id"] for hit in events for e in hit["evidence"]}
                    ),
                },
                "debug": None,
            },
        }
    ).model_dump(mode="json")
