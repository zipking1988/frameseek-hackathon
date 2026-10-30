from frameseek.demo import catalog


def test_review_routes_have_valid_evidence_and_scoped_continuations():
    data = catalog()
    queries = {query["id"]: query for query in data["queries"]}
    assert len(queries) == 17
    assert len(data["scenarios"]) == 6
    assert len({route["id"] for route in data["scenarios"]}) == 6
    for query in queries.values():
        assert query["suggested_replies"] == list(query["followup_map"])
        assert all(target in queries for target in query["followup_map"].values())
        for event in query["response"]["events"]:
            assert 0 <= event["start"] < event["end"] <= data["video"]["duration"]
            assert all(event["start"] <= frame["t"] < event["end"] for frame in event["evidence"])
    for route in data["scenarios"]:
        assert len(route["steps"]) == 3
        for index, step in enumerate(route["steps"]):
            assert step["query_id"] in queries
            assert step["parent"] is None or 0 <= step["parent"] < index
            if step["parent"] is not None:
                parent_query = queries[route["steps"][step["parent"]]["query_id"]]
                question = step.get("question", queries[step["query_id"]]["question"])
                assert parent_query["followup_map"][question] == step["query_id"]
    boundary = next(route for route in data["scenarios"] if route["id"] == "boundaries")
    assert [queries[step["query_id"]]["response"]["status"] for step in boundary["steps"]] == [
        "no_result",
        "error",
        "ok",
    ]


def test_basketball_catalog_uses_only_its_own_english_frames():
    import re

    data = catalog("demo_basketball")
    queries = {query["id"]: query for query in data["queries"]}
    assert data["synthetic"] and data["mode"] == "fixture"
    assert data["video"]["id"] == "demo_basketball"
    assert queries["shot"]["followup_map"]["Does the shot score?"] == "outcome"
    assert queries["outcome"]["response"]["status"] == "partial"
    assert "does not show whether it scores" in queries["outcome"]["response"]["answer"]
    for query in queries.values():
        assert not re.search(r"[\u4e00-\u9fff]", str(query))
        for event in query["response"]["events"]:
            assert event["video_id"] == "demo_basketball"
            for frame in event["evidence"]:
                assert event["start"] <= frame["t"] < event["end"]
                assert frame["uri"].startswith("/media/demo_basketball/frames/")


def test_basketball_frames_match_their_video_timestamps():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required to check the encoded demo")
    base = Path(__file__).resolve().parents[1] / "src/frameseek/static/basketball_demo"
    for second in (0, 6, 13):
        decoded = subprocess.check_output(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(second),
                "-i",
                str(base / "basketball.mp4"),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
        )
        assert decoded == (base / "frames" / f"{second}.jpg").read_bytes()
