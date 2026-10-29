"""Private review-page preparation uses synthetic media only."""

import copy
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_knowledge_compiler import decode, infer, setup_store

from frameseek.knowledge_compiler import compile_pending
from frameseek.knowledge_evaluation import prepare_review_case
from frameseek.workspace import Workspace

spec = importlib.util.spec_from_file_location(
    "prepare_knowledge_review", Path(__file__).parents[1] / "scripts/prepare_knowledge_review.py"
)
review_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_tool)
load_review_data = review_tool.load_review_data
render_review_page = review_tool.render_review_page
score_review_data = review_tool.score_review_data


def fixture_workspace(tmp_path):
    root = tmp_path / "workspace"
    ws = Workspace(root)
    directory = root / "vid_fixture"
    directory.mkdir()
    store = setup_store(directory)

    def synthetic_jpeg(*args):
        rows = decode(*args)
        for row in rows:
            (directory / "frames" / row["file"]).write_bytes(b"\xff\xd8\xffsynthetic\xff\xd9")
        return rows

    version = compile_pending(store, {"duration": 16}, infer, lambda: True, decode=synthetic_jpeg)
    expiry = time.time() + 3600
    with ws.db() as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?)", ("owner", "csrf", expiry))
        c.execute(
            "INSERT INTO videos(id,owner,name,bytes,generation,metadata,expires) "
            "VALUES(?,?,?,?,?,?,?)",
            ("vid_fixture", "owner", "synthetic.mp4", 10, 1, json.dumps({"duration": 16}), expiry),
        )
        c.execute(
            "INSERT INTO knowledge_jobs(video,generation,state,completed,total,version,"
            "updated,cache_key) "
            "VALUES(?,1,'ready',2,2,?,?,?)",
            ("vid_fixture", version, time.time(), store.key),
        )
    index = store.read_version(version).publishable_index()
    case = prepare_review_case(
        index,
        index_version=version,
        source_kind="synthetic",
        case_id="case-1",
        question="find a person",
        start=0,
        end=8,
        categories=["person"],
    )
    return root, directory, case


def test_review_page_loads_only_exact_owned_unlabelled_frames(tmp_path):
    root, _, case = fixture_workspace(tmp_path)
    data = load_review_data(root, [case])
    assert len(data[0]["images"]) == 2
    assert data[0]["case"]["reviewed"] is False
    assert data[0]["case"]["predicted"] is None
    altered = copy.deepcopy(case)
    altered["frame_references"][0]["t"] = 9
    with pytest.raises(ValueError, match="review_packet_not_matching_index"):
        load_review_data(root, [altered])
    altered = copy.deepcopy(case)
    altered["reviewed"] = True
    with pytest.raises(ValueError, match="review_packet_must_be_unlabelled"):
        load_review_data(root, [altered])


def test_review_page_escapes_untrusted_text_and_disables_external_network():
    page = render_review_page(
        [{"case": {"question": "</script><img src=https://example.test>"}, "images": []}]
    )
    assert "</script><img" not in page
    assert "\\u003c/script>" in page
    assert "default-src 'none'" in page and "img-src data:" in page
    assert "predicted:null" in page


def test_review_page_rejects_expired_source_and_symlinked_frame(tmp_path):
    root, directory, case = fixture_workspace(tmp_path)
    frame = directory / "frames" / case["frame_references"][0]["uri"].rsplit("/", 1)[-1]
    frame.unlink()
    frame.symlink_to(tmp_path / "other.jpg")
    with pytest.raises(ValueError, match="invalid_review_frame"):
        load_review_data(root, [case])
    ws = Workspace(root)
    with ws.db() as c:
        c.execute("UPDATE videos SET expires=0")
    with pytest.raises(ValueError, match="review_source_unavailable_or_foreign"):
        load_review_data(root, [case])


def test_review_page_limits_embedded_frame_count(tmp_path, monkeypatch):
    root, _, case = fixture_workspace(tmp_path)
    monkeypatch.setattr(review_tool, "MAX_REVIEW_FRAMES", 1)
    with pytest.raises(ValueError, match="review_frame_limit"):
        load_review_data(root, [case])


def test_scoring_recomputes_predictions_and_rejects_changed_cases(tmp_path):
    root, _, case = fixture_workspace(tmp_path)
    pending = score_review_data(root, [case], [case])
    assert not pending["accepted"] and pending["cases"] == 0
    assert pending["prediction_source"] == "recomputed_sampled_index"
    reviewed = copy.deepcopy(case)
    reviewed["reviewed"] = True
    reviewed["reviewer"] = "synthetic test only"
    reviewed["visible_duration_seconds"] = 1
    reviewed["frame_labels"][0]["visible"] = True
    reviewed["frame_labels"][1]["visible"] = False
    reviewed["expected"] = [reviewed["sampled_frames"][0]]
    report = score_review_data(root, [case], [reviewed])
    assert report["groups"]["synthetic"]["tp"] == 1
    assert report["groups"]["synthetic"]["fp"] == 1
    assert report["model_requests_sent"] == 0 and not report["accepted"]
    assert report["acceptance_scope"] == "reviewed_sampled_index_only"
    assert "production_model_quality_not_scored" in report["limitations"]
    forged = copy.deepcopy(reviewed)
    forged["predicted"] = [reviewed["sampled_frames"][0]]
    with pytest.raises(ValueError, match="review_case_changed"):
        score_review_data(root, [case], [forged])
    forged = copy.deepcopy(reviewed)
    forged["question"] = "different question"
    with pytest.raises(ValueError, match="review_case_changed"):
        score_review_data(root, [case], [forged])
    with pytest.raises(ValueError, match="review_case_set_mismatch"):
        score_review_data(root, [case], [])


def test_scoring_cli_writes_private_nonaccepted_report_without_overwriting(tmp_path):
    root, _, case = fixture_workspace(tmp_path)
    original, reviewed, output = (
        tmp_path / name for name in ("cases.json", "reviewed.json", "report.json")
    )
    original.write_text(json.dumps([case]))
    reviewed.write_text(json.dumps([case]))
    script = Path(__file__).parents[1] / "scripts/score_knowledge_review.py"
    command = [
        sys.executable,
        str(script),
        "--workspace",
        str(root),
        "--cases",
        str(original),
        "--reviewed",
        str(reviewed),
        "--output",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=15)
    report = json.loads(output.read_text())
    assert not report["accepted"] and report["model_requests_sent"] == 0
    assert output.stat().st_mode & 0o777 == 0o600
    second = subprocess.run(command, capture_output=True, timeout=15)
    assert second.returncode != 0 and json.loads(output.read_text()) == report
