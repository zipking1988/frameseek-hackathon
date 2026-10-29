"""Pure window planning on original-video seconds; never truncate the tail."""

import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from itertools import pairwise

_TIME_RANGE = re.compile(
    r"(?:(?P<prefix>\bbetween\b|\bfrom\b|请在|在)\s*)?"
    r"(?<![\d:])(?P<start>\d{1,3}:[0-5]\d)\s*"
    r"(?P<separator>至|到|[-–—~～]|\bto\b|\band\b)\s*"
    r"(?P<end>\d{1,3}:[0-5]\d)(?![\d:])",
    re.I,
)

# Also recognize ranges with invalid seconds so callers can reject them rather
# than silently interpreting the question as an unscoped video search.
_RANGE_LIKE = re.compile(
    r"(?:(?P<prefix>\bbetween\b|\bfrom\b|请在|在)\s*)?"
    r"(?<![\d:])(?P<start>\d{1,3}:\d{2})\s*"
    r"(?P<separator>至|到|[-–—~～]|\bto\b|\band\b)\s*"
    r"(?P<end>\d{1,3}:\d{2})(?![\d:])",
    re.I,
)

_QUOTED_TEXT = re.compile(r'“[^”\n]*”|"[^"\n]*"|‘[^’\n]*’')
_SINGLE_TIME = re.compile(
    r"(?:\bat\s+|(?:请)?在\s*)(?P<time>\d{1,3}:[0-5]\d)(?![\d:])"
    r"(?!\s*(?:前|后|之前|之后|以前|以后|左右|附近|至|到|"
    r"\b(?:or\s+)?(?:before|after|earlier|later)\b))",
    re.I,
)


def mask_quoted_text(text):
    """Hide quoted visible text while preserving offsets into the original question."""
    return _QUOTED_TEXT.sub(lambda match: " " * len(match.group()), text)


def find_time_range(text):
    """Find a range; 'and' is a range separator only following 'between'."""
    for match in _TIME_RANGE.finditer(mask_quoted_text(text)):
        if (
            match.group("separator").lower() == "and"
            and (match.group("prefix") or "").lower() != "between"
        ):
            continue
        return match
    return None


def find_single_time(text):
    """Recognize a time explicitly introduced by 'at' or '在', outside quotes."""
    masked = mask_quoted_text(text)
    for match in _SINGLE_TIME.finditer(masked):
        if re.search(r"(?:不|未|没|\bnot\s+)\s*$", masked[: match.start()], re.I):
            continue
        return match
    return None


def has_malformed_time_range(text):
    """Detect an explicit range with seconds outside 00–59, excluding quoted text."""
    for match in _RANGE_LIKE.finditer(mask_quoted_text(text)):
        if (
            match.group("separator").lower() == "and"
            and (match.group("prefix") or "").lower() != "between"
        ):
            continue
        if any(int(match.group(name).split(":")[1]) >= 60 for name in ("start", "end")):
            return True
    return False


@dataclass(frozen=True)
class Window:
    index: int
    start: float
    end: float


def plan_windows(duration: float, scene_boundaries: tuple[float, ...] = ()) -> list[Window]:
    if (
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or not 0 < duration <= 3600
    ):
        raise ValueError("duration must be finite and between 0 and 3600 seconds")
    boundaries = tuple(scene_boundaries)
    if len(boundaries) > 10000:
        raise ValueError("scene boundary list exceeds finite admission limit")
    if any(
        type(t) not in (int, float) or not math.isfinite(t) or not 0 < t < duration
        for t in boundaries
    ):
        raise ValueError("scene boundaries must be finite and inside the video")
    if any(b <= a for a, b in pairwise(boundaries)):
        raise ValueError("scene boundaries must be strictly increasing")
    result: list[Window] = []
    start = 0.0
    while start < duration:
        hard_end = min(start + 120, duration)
        # A cut must advance by more than overlap, or a short scene could stall the loop.
        cut_index = bisect_right(boundaries, hard_end) - 1
        cut = boundaries[cut_index] if cut_index >= 0 else 0
        end = cut if cut > start + 5 else hard_end
        result.append(Window(len(result), start, end))
        if end == duration:
            break
        start = end - 5
    return result
