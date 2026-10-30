"""Resolve explicit continuation commands only, after caller validates the anchor."""

import re

from frameseek.windows import find_single_time, find_time_range

CONTINUATION = re.compile(
    r"\s*(?:继续(?:这个|这条|刚才的)?(?:问题|提问)?|接着这个问题|同样的问题|"
    r"continue(?: this question)?|same question)[？?。.!！\s]*",
    re.I,
)
PRONOUN_TIME = re.compile(
    r"\s*(?:[它他她](?:们)?(出现在哪些时间|出现的时间|出现时间|什么时候出现|在哪些时间出现)|"
    r"(?:when|where)\s+(?:(?:does|did)\s+(?:it|he|she)|(?:do|did)\s+they)\s+"
    r"(?:appear|show\s+up))[？?。.!！\s]*",
    re.I,
)


def resolve_followup(question, context):
    pronoun = PRONOUN_TIME.fullmatch(question)
    if pronoun:
        previous = (context or {}).get("question", "")
        if not previous:
            raise ValueError("followup_question_missing")
        from frameseek.semantic_search import VOCABULARY, interpret, remove_time_range

        prior_visual = (
            remove_time_range(previous)
            if find_time_range(previous) or find_single_time(previous)
            else previous
        )
        terms, problems = interpret(prior_visual)
        subjects = set(terms) & {"person", "parcel", "car", "bicycle"}
        if problems or len(subjects) != 1 or any(term not in VOCABULARY for term in terms):
            raise ValueError("followup_context_ambiguous")
        if re.search(r"\b(?:he|she)\b", question, re.I) and subjects != {"person"}:
            raise ValueError("followup_context_ambiguous")
        # Reuse the description, not a presumed cross-frame identity.
        return previous + ("，" + pronoun.group(1) if pronoun.group(1) else ", all times")
    if not CONTINUATION.fullmatch(question):
        return question
    previous = (context or {}).get("question", "")
    if not previous or CONTINUATION.fullmatch(previous):
        raise ValueError("followup_question_missing")
    return previous


def matches_source_generation(result, video, generation):
    """Legacy answers lack a generation; only the initial generation is unambiguous."""
    if result.get("video_id") != video:
        return False
    if "source_generation" not in result:
        return generation == 1
    saved_generation = result["source_generation"]
    return type(saved_generation) is int and saved_generation == generation


def saved_question(result, request_input, load_parent, *, video, generation):
    """Recover older continuation chains without rewriting historical rows."""
    seen = set()
    for _ in range(10):
        if not matches_source_generation(result, video, generation):
            raise ValueError("stale_context_generation")
        question = (
            result.get("resolved_question")
            or result.get("index_search", {}).get("resolved_question")
            or result["question"]
        )
        try:
            resolve_followup(question, None)
            return question
        except ValueError:
            parent = request_input.get("parent_request_id")
            if not parent or parent in seen:
                raise ValueError("followup_question_missing") from None
            seen.add(parent)
            row = load_parent(parent)
            if row is None:
                raise ValueError("invalid_context_anchor") from None
            result, request_input = row
    raise ValueError("followup_context_depth")
