"""Build a private, offline human-review page from an owned index packet.

No model is loaded or called. Images and annotations remain local; generated
pages contain private media and must not be published. Predictions are never
embedded in the blinded review page.
"""

import argparse
import base64
import json
import os
import sqlite3
import time
from pathlib import Path

from frameseek.index_service import source_digest
from frameseek.knowledge_evaluation import (
    catalog_from_index,
    prepare_review_case,
    sampled_index_predictions,
    score_cases,
)
from frameseek.video_knowledge import load_published_state

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PACKET_BYTES = 10 * 1024 * 1024
MAX_REVIEW_FRAMES = 500
MAX_REVIEW_IMAGE_BYTES = 64 * 1024 * 1024


def load_review_data(workspace, packet, *, include_index=False):
    """Resolve packet references through active owner/source/version records."""
    workspace = Path(workspace).resolve()
    path = workspace / "workspace.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid_review_workspace")
    if not isinstance(packet, list) or not 1 <= len(packet) <= 200:
        raise ValueError("invalid_review_packet")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    loaded, output, ids, owner = {}, [], set(), None
    frame_count, image_bytes = 0, 0
    try:
        for case in packet:
            if not isinstance(case, dict) or case.get("reviewed") is not False:
                raise ValueError("review_packet_must_be_unlabelled")
            source = connection.execute(
                "SELECT v.*,j.cache_key FROM videos v JOIN knowledge_jobs j "
                "ON j.video=v.id AND j.generation=v.generation "
                "JOIN sessions s ON s.id=v.owner "
                "WHERE v.id=? AND v.deleted=0 AND v.expires>? AND s.expires>? "
                "AND j.state='ready'",
                (case.get("source_id"), time.time(), time.time()),
            ).fetchone()
            if not source or (owner is not None and source["owner"] != owner):
                raise ValueError("review_source_unavailable_or_foreign")
            owner = source["owner"]
            version = case.get("index_version")
            key = (source["id"], version)
            if key not in loaded:
                directory = workspace / source["id"]
                if (
                    directory.is_symlink()
                    or not directory.is_dir()
                    or directory.resolve().parent != workspace
                ):
                    raise ValueError("invalid_review_source")
                state = load_published_state(
                    directory,
                    source["id"],
                    source["generation"],
                    source["cache_key"],
                    version,
                    source_digest(directory),
                )
                loaded[key] = (directory, state.publishable_index())
            directory, index = loaded[key]
            expected = prepare_review_case(
                index,
                index_version=version,
                source_kind=case.get("source_kind"),
                case_id=case.get("id"),
                question=case.get("question"),
                start=case.get("scope_start"),
                end=case.get("scope_end"),
                categories=case.get("categories"),
            )
            if case.get("id") in ids or any(case.get(k) != expected[k] for k in expected):
                raise ValueError("review_packet_not_matching_index")
            ids.add(case["id"])
            catalog = {frame.frame_id: frame for frame in index.frames}
            images = []
            frames_dir = directory / "frames"
            if (
                frames_dir.is_symlink()
                or not frames_dir.is_dir()
                or frames_dir.resolve().parent != directory
            ):
                raise ValueError("invalid_review_frame_directory")
            for frame_id in expected["sampled_frames"]:
                frame_count += 1
                if frame_count > MAX_REVIEW_FRAMES:
                    raise ValueError("review_frame_limit")
                frame = catalog[frame_id]
                file = directory / "frames" / frame.file
                if file.is_symlink() or file.resolve().parent != frames_dir.resolve():
                    raise ValueError("invalid_review_frame")
                fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    raw = stream.read(MAX_IMAGE_BYTES + 1)
                if len(raw) > MAX_IMAGE_BYTES or not raw.startswith(b"\xff\xd8\xff"):
                    raise ValueError("invalid_review_frame")
                image_bytes += len(raw)
                if image_bytes > MAX_REVIEW_IMAGE_BYTES:
                    raise ValueError("review_image_bytes_limit")
                images.append(base64.b64encode(raw).decode("ascii"))
            entry = {"case": expected, "images": images}
            if include_index:
                entry["index"] = index
            output.append(entry)
    finally:
        connection.close()
    return output


def score_review_data(workspace, packet, reviewed):
    """Recompute index results against the original packet, never trust predictions."""
    entries = load_review_data(workspace, packet, include_index=True)
    if not isinstance(reviewed, list) or len(reviewed) != len(entries):
        raise ValueError("review_case_set_mismatch")
    if any(not isinstance(case, dict) or not isinstance(case.get("id"), str) for case in reviewed):
        raise ValueError("review_case_set_mismatch")
    by_id = {case["id"]: case for case in reviewed}
    if len(by_id) != len(reviewed) or set(by_id) != {entry["case"]["id"] for entry in entries}:
        raise ValueError("review_case_set_mismatch")
    mutable = {"frame_labels", "expected", "reviewed", "reviewer", "visible_duration_seconds"}
    additional = {"followup_check", "seek_checks"}
    sources, scored, unscored = {}, [], []
    for entry in entries:
        original, index = entry["case"], entry["index"]
        result = by_id[original["id"]]
        if set(result) - set(original) - additional or any(
            result.get(key) != value for key, value in original.items() if key not in mutable
        ):
            raise ValueError("review_case_changed")
        source = catalog_from_index(
            index, index_version=original["index_version"], source_kind=original["source_kind"]
        )
        for key, value in source.items():
            if key in sources and sources[key] != value:
                raise ValueError("review_multiple_source_versions")
            sources[key] = value
        prediction = sampled_index_predictions(
            index,
            index_version=original["index_version"],
            source_kind=original["source_kind"],
            question=original["question"],
            start=original["scope_start"],
            end=original["scope_end"],
        )
        if prediction["kind"] != "sampled_index_predictions":
            unscored.append({"id": original["id"], "reason": prediction["kind"]})
            continue
        scored.append({**result, "predicted": prediction["predicted"]})
    report = score_cases(scored, source_catalog=sources)
    if unscored:
        report["accepted"] = False
    return {
        **report,
        "unscored_cases": unscored,
        "prediction_source": "recomputed_sampled_index",
        "acceptance_scope": "reviewed_sampled_index_only",
        "model_requests_sent": 0,
        "limitations": [
            "reviewer_identity_self_attested",
            "source_kind_self_attested",
            "browser_journey_not_scored",
            "production_model_quality_not_scored",
        ],
    }


def render_review_page(data):
    """Render a network-isolated page; all untrusted text goes through textContent."""
    encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    template = Path(__file__).with_name("knowledge_review_template.html").read_text()
    return template.replace("DATA_PLACEHOLDER", encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.cases)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        encoded = stream.read(MAX_PACKET_BYTES + 1)
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError("invalid_review_packet")
    packet = json.loads(encoded)
    page = render_review_page(load_review_data(args.workspace, packet)).encode()
    output = Path(args.output)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(page)
    print("Private review page created; no model requests were sent.")


if __name__ == "__main__":
    main()
