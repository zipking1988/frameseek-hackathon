"""Deterministic, zero-dispatch routing policy over owned evidence/capabilities."""

import re


def needs_audio(question):
    return bool(
        re.search(
            r"音轨|解说|声音|说了什么|说的什么|\baudio\b|\bspeech\b|\bsaid\b|\btranscri",
            question,
            re.I,
        )
    )


def sufficient_index(result):
    if result.get("kind") != "indexed_occurrences":
        return False
    coverage = result.get("coverage", {})
    if not coverage.get("all_source_frames_reviewed") or not coverage.get("exhaustive_under_index"):
        return False
    stats = result.get("aggregation")
    if stats:
        return bool(stats.get("exact_under_index") and stats.get("ranking_complete", True))
    return result.get("structured_conditions_evaluated") is True


def choose_engine(question, result, *, local_ready, cloud_ready):
    if needs_audio(question):
        return "model", "正在检查音轨和转写能力；不会用静态画面猜测声音。"
    if sufficient_index(result):
        return "index", "已有结构化证据覆盖本次条件，将直接检索，不调用模型。"
    if local_ready:
        return "local", "已有证据不足，正在使用本机视觉分析，不消耗云端额度。"
    if cloud_ready:
        return "cloud", "已有证据不足，正在检查云端分析预算；通过后才会处理视频。"
    return "model", "已有证据不足，正在检查可用模型与预算；不会自动重试。"
