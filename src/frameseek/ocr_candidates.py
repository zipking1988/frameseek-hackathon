"""Bounded, unverified OCR leads from already indexed private frames."""

import re
import shutil
import tempfile
import time
from pathlib import Path

from frameseek.agent import AgentError, explicit_time_window
from frameseek.media import MediaError, run
from frameseek.occurrences import normalized
from frameseek.semantic_search import interpret, remove_time_range


def request_scope(question, duration):
    """Accept only a literal text question with a short explicit time range."""
    try:
        scope = explicit_time_window(question, duration, max_seconds=8)
    except AgentError as error:
        raise ValueError("ocr_requires_short_time_range") from error
    if not scope:
        raise ValueError("ocr_requires_short_time_range")
    stripped = remove_time_range(question)
    match = re.search(
        r"(?:文字|文本|text)\s*(?:包含|是|为|contains|[:：])?\s*"
        r'(?:“([^”]{1,200})”|"([^"\n]{1,200})"|‘([^’]{1,200})’)',
        stripped,
        re.I,
    )
    if not match:
        raise ValueError("ocr_requires_quoted_text")
    terms, problems = interpret(stripped[: match.start()] + " " + stripped[match.end() :])
    if problems or terms:
        raise ValueError("ocr_requires_text_only")
    literal = next(value for value in match.groups() if value is not None)
    if not normalized(literal):
        raise ValueError("ocr_requires_quoted_text")
    # This processor invokes Tesseract's English language data only. Passing
    # other scripts would misleadingly report a completed search with no hits.
    if not literal.isascii():
        raise ValueError("ocr_english_text_only")
    return scope, literal


def eligible_index_scope(index, question):
    """Check question and sampled-frame bounds without running OCR."""
    if index is None:
        return False
    try:
        scope, _ = request_scope(question, index.duration)
    except ValueError:
        return False
    return 1 <= sum(scope["start"] <= frame.t < scope["end"] for frame in index.frames) <= 8


def find_candidates(directory, index, question, *, binary=None, runner=run):
    """Return only candidate frame references; never assert verified text facts."""
    scope, literal = request_scope(question, index.duration)
    binary = binary or shutil.which("tesseract")
    if not binary or not Path(binary).is_absolute():
        raise ValueError("ocr_unavailable")
    directory = Path(directory).resolve()
    raw_frames_dir = directory / "frames"
    if raw_frames_dir.is_symlink():
        raise ValueError("indexed_evidence_missing")
    frames_dir = raw_frames_dir.resolve()
    selected = [frame for frame in index.frames if scope["start"] <= frame.t < scope["end"]]
    if not 1 <= len(selected) <= 8:
        raise ValueError("ocr_frame_limit")
    output, failed = [], 0
    deadline = time.monotonic() + 20
    for frame in selected:
        path = directory / "frames" / frame.file
        if path.is_symlink() or not path.is_file() or path.resolve().parent != frames_dir:
            raise ValueError("indexed_evidence_missing")
        try:
            matched = False
            for mode in ("11", "6"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaError("ocr_time_limit")
                text, _ = runner(
                    directory,
                    [binary, str(path), "stdout", "-l", "eng", "--psm", mode],
                    timeout=min(4, remaining),
                    output_limit=65536,
                )
                if normalized(literal) in normalized(text):
                    matched = True
                    break
            # A bounded full-frame enlargement can expose small text without
            # assuming where an overlay or subject appears in a video.
            if not matched and (ffmpeg := shutil.which("ffmpeg")):
                with tempfile.TemporaryDirectory(prefix=".ocr-", dir=directory) as temporary:
                    scaled = Path(temporary) / "scaled.png"
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError("ocr_time_limit")
                    runner(
                        directory,
                        [
                            ffmpeg,
                            "-nostdin",
                            "-v",
                            "error",
                            "-i",
                            str(path),
                            "-vf",
                            "scale=w='min(2*iw,2560)':h='min(2*ih,2560)':force_original_aspect_ratio=decrease",
                            "-frames:v",
                            "1",
                            str(scaled),
                        ],
                        timeout=min(4, remaining),
                        output_limit=65536,
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError("ocr_time_limit")
                    text, _ = runner(
                        directory,
                        [binary, str(scaled), "stdout", "-l", "eng", "--psm", "11"],
                        timeout=min(4, remaining),
                        output_limit=65536,
                    )
                    matched = normalized(literal) in normalized(text)
        except MediaError:
            failed += 1
            continue
        if matched:
            output.append({"frame_id": frame.frame_id, "t": frame.t, "uri": frame.uri})
    return {
        "kind": "ocr_candidates",
        "scope": scope,
        "frames": output,
        "checked_frames": len(selected) - failed,
        "failed_frames": failed,
        "verified_matches": False,
        "model_requests_sent": 0,
    }
