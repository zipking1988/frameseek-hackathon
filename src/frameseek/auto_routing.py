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
        return (
            "model",
            (
                "Checking audio and transcription capabilities; sound will "
                "not be inferred from still frames."
            ),
        )
    if sufficient_index(result):
        return (
            "index",
            "Existing structured evidence covers these conditions; search will not call a model.",
        )
    if local_ready:
        return (
            "local",
            "Existing evidence is insufficient; local visual analysis will not use cloud budget.",
        )
    if cloud_ready:
        return (
            "cloud",
            "Existing evidence is insufficient; cloud budget is checked before processing.",
        )
    return (
        "model",
        (
            "Existing evidence is insufficient; checking model "
            "availability and budget. No automatic retry."
        ),
    )
