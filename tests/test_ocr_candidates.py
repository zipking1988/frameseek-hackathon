"""OCR is a bounded unverified lead, never an observation or provider call."""

import pytest
from test_occurrences import catalog

from frameseek.media import MediaError
from frameseek.ocr_candidates import eligible_index_scope, find_candidates, request_scope


def test_scoped_ocr_only_reports_matching_frame_refs_and_failures(tmp_path, monkeypatch):
    import frameseek.ocr_candidates as ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _: None)
    directory = tmp_path / "video"
    frames = directory / "frames"
    frames.mkdir(parents=True)
    index = catalog(8)
    for frame in index.frames:
        (frames / frame.file).write_bytes(b"fake jpeg")
    calls = []

    def fake_run(root, args, timeout, output_limit):
        calls.append(args)
        assert root == directory.resolve() and 0 < timeout <= 4 and output_limit == 65536
        if args[1].endswith("f2.jpg"):
            raise MediaError("media_timeout")
        return (
            "EXIT"
            if args[1].endswith("f1.jpg") or args[1].endswith("f3.jpg") and args[-1] == "6"
            else "nothing",
            "",
        )

    result = find_candidates(
        directory,
        index,
        "请在00:00至00:04找出文字“EXIT”",
        binary="/usr/bin/tesseract",
        runner=fake_run,
    )
    assert [x["t"] for x in result["frames"]] == [1, 3]
    assert result["checked_frames"] == 3 and result["failed_frames"] == 1
    assert result["verified_matches"] is False and result["model_requests_sent"] == 0
    assert len(calls) == 6


def test_enlargement_is_bounded_temporary_and_returns_original_frame(tmp_path, monkeypatch):
    import frameseek.ocr_candidates as ocr

    directory = tmp_path / "video"
    frames = directory / "frames"
    frames.mkdir(parents=True)
    (frames / "f0.jpg").write_bytes(b"fake jpeg")
    monkeypatch.setattr(
        ocr.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )
    calls = []

    def fake_run(root, args, **kwargs):
        calls.append(args)
        if args[0] == "/usr/bin/ffmpeg":
            assert "2560" in args[args.index("-vf") + 1]
            return "", ""
        return ("EXIT" if args[1].endswith("scaled.png") else "", "")

    result = find_candidates(
        directory,
        catalog(1),
        "请在00:00至00:01找出文字“EXIT”",
        binary="/usr/bin/tesseract",
        runner=fake_run,
    )
    assert [item["uri"] for item in result["frames"]] == ["/media/vid_offline/frames/f0.jpg"]
    assert len(calls) == 4 and not list(directory.glob(".ocr-*"))


def test_ocr_requires_literal_text_and_short_explicit_scope():
    for question in (
        'Find text "EXIT" from 00:04 to 00:06',
        'Find text "EXIT" between 00:04 and 00:06',
    ):
        assert request_scope(question, 12) == ({"start": 4, "end": 6}, "EXIT")
    assert request_scope('Find text "EXIT" at 00:04', 12) == (
        {"start": 4, "end": 5},
        "EXIT",
    )
    assert request_scope("在00:04找文字“EXIT”", 12) == (
        {"start": 4, "end": 5},
        "EXIT",
    )
    assert request_scope('Find text "00:04 to 00:06" from 00:08 to 00:10', 12) == (
        {"start": 8, "end": 10},
        "00:04 to 00:06",
    )
    for question in (
        "找出文字“EXIT”",
        "请在00:00至00:09找出文字“EXIT”",
        "请在00:00至00:02找出EXIT",
        "请在00:00至00:02找出红色文字“EXIT”",
        'Find text "EXIT" 00:04 and 00:06',
    ):
        with pytest.raises(ValueError):
            request_scope(question, 12)


def test_ocr_eligibility_requires_a_bounded_nonempty_indexed_frame_set():
    from types import SimpleNamespace

    question = 'Find text "EXIT" at 00:04'
    assert not eligible_index_scope(None, question)
    sparse = SimpleNamespace(duration=12, frames=[SimpleNamespace(t=2), SimpleNamespace(t=6)])
    assert not eligible_index_scope(sparse, question)
    sparse.frames.append(SimpleNamespace(t=4.5))
    assert eligible_index_scope(sparse, question)
    sparse.frames.extend(SimpleNamespace(t=4 + i / 100) for i in range(8))
    assert not eligible_index_scope(sparse, question)


def test_english_ocr_does_not_misrepresent_other_scripts_as_searched():
    index = catalog(8)
    for literal in ("出口", "出口EXIT", "école"):
        question = f'Find text "{literal}" at 00:04'
        assert not eligible_index_scope(index, question)
        with pytest.raises(ValueError, match="ocr_english_text_only"):
            request_scope(question, index.duration)


def test_ocr_deadline_reports_unprocessed_frames_as_failures(tmp_path, monkeypatch):
    import frameseek.ocr_candidates as ocr

    directory = tmp_path / "video"
    frames = directory / "frames"
    frames.mkdir(parents=True)
    index = catalog(2)
    for frame in index.frames:
        (frames / frame.file).write_bytes(b"fake jpeg")
    ticks = iter((0, 21, 21))
    monkeypatch.setattr(ocr.time, "monotonic", lambda: next(ticks))
    result = find_candidates(
        directory,
        index,
        "请在00:00至00:02找出文字“EXIT”",
        binary="/usr/bin/tesseract",
        runner=lambda *a, **k: pytest.fail("deadline reached"),
    )
    assert result["checked_frames"] == 0 and result["failed_frames"] == 2


def test_ocr_does_not_follow_foreign_or_symlinked_frames(tmp_path):
    directory = tmp_path / "video"
    frames = directory / "frames"
    frames.mkdir(parents=True)
    (tmp_path / "outside").write_bytes(b"not an image")
    (frames / "f0.jpg").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="indexed_evidence_missing"):
        find_candidates(
            directory,
            catalog(1),
            "请在00:00至00:01找出文字“EXIT”",
            binary="/usr/bin/tesseract",
            runner=lambda *a, **k: pytest.fail("called"),
        )
