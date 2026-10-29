"""Open-vocabulary search/grounding contracts, not live recognition claims."""

import copy

import pytest

from frameseek.visual_search import (
    SearchError,
    checked_matches,
    compile_index,
    retrieve,
    verification_prompt,
)


def source(captions=None):
    captions = captions or ["A person moves a red parcel", "A blue car waits", "A person waves"]
    return {
        "generation": 2,
        "segments": [
            {
                "frames": [
                    {
                        "frame_id": f"vid_fixture_f{i}",
                        "file": f"f{i}.jpg",
                        "uri": f"/media/vid_fixture/frames/f{i}.jpg",
                        "t": float(i * 10),
                        "caption": caption,
                    }
                    for i, caption in enumerate(captions)
                ]
            }
        ],
    }


def payload(frames, *, verdict="yes", subject="personA"):
    return {
        "criteria": [
            {"id": "c1", "description": "required appearance"},
            {"id": "c2", "description": "required action"},
        ],
        "matches": [
            {
                "frame_id": frame["frame_id"],
                "subject_id": subject,
                "statement": "合成观察：条件在同一画面主体上成立。",
                "checks": [
                    {"criterion_id": key, "subject_id": subject, "verdict": verdict}
                    for key in ("c1", "c2")
                ],
            }
            for frame in frames
        ],
        "unresolved": [],
        "no_match": False,
    }


@pytest.mark.parametrize(
    "question,captions,target",
    [
        ("找红帽子白衣服的人", ["蓝色车", "红帽子白衣服的人在门口", "纸箱"], 1),
        ("Where is a yellow parcel?", ["A blue car", "A person", "A yellow parcel near a door"], 2),
        ("找自行车", ["一个人", "自行车靠在墙边", "纸箱"], 1),
    ],
)
def test_same_index_retrieval_for_different_videos_and_attributes(question, captions, target):
    index = compile_index("vid_fixture", 2, 40, source(captions))
    result = retrieve(index, question, limit=1)
    assert result["frames"][0]["frame_id"] == f"vid_fixture_f{target}"
    assert index["complete_object_inventory"] is False
    assert (
        index["fingerprint"] == compile_index("vid_fixture", 2, 40, source(captions))["fingerprint"]
    )
    assert question in verification_prompt(question)


def test_all_frames_ranked_instead_of_three_broad_segments_and_no_silent_exhaustiveness():
    captions = [f"unrelated scene {i}" for i in range(40)]
    captions[37] = "unusual copper statue near the exit"
    result = retrieve(compile_index("vid_fixture", 2, 400, source(captions)), "copper statue")
    assert result["indexed_frames"] == 40 and len(result["frames"]) == 8
    assert "vid_fixture_f37" in {f["frame_id"] for f in result["frames"]}
    assert result["coverage"] == "candidate_samples_only"
    assert len({f["t"] for f in result["frames"]}) == 8


def test_semantically_unmatched_caption_still_gets_temporal_exploration():
    index = compile_index("vid_fixture", 2, 40, source())
    assert len(retrieve(index, "unknown paraphrase")["frames"]) == 3


def test_conjunction_cannot_combine_different_people_or_omit_a_criterion():
    frames = source()["segments"][0]["frames"][:1]
    response = payload(frames)
    response["matches"][0]["checks"][1]["subject_id"] = "personB"
    with pytest.raises(SearchError, match="conjunction"):
        checked_matches(response, frames, 40)
    response = payload(frames)
    response["matches"][0]["checks"].pop()
    with pytest.raises(SearchError):
        checked_matches(response, frames, 40)


def test_unclear_and_negative_checks_are_not_hits():
    frames = source()["segments"][0]["frames"][:1]
    response = payload(frames, verdict="uncertain")
    response["no_match"] = True
    observations, warnings = checked_matches(response, frames, 40)
    assert observations == [] and warnings
    response = payload(frames, verdict="no")
    response["no_match"] = True
    assert checked_matches(response, frames, 40)[0] == []


def test_separate_timeline_evidence_never_spans_unseen_gap():
    frames = source()["segments"][0]["frames"]
    observations, _ = checked_matches(payload(frames), frames, 40)
    assert [o["start"] for o in observations] == [0, 10, 20]
    assert all(o["end"] - o["start"] <= 0.0011 for o in observations)
    assert all(len(o["frame_ids"]) == 1 for o in observations)


@pytest.mark.parametrize("change", ["foreign", "path", "timestamp", "conflicting", "generation"])
def test_index_rejects_foreign_unsafe_and_stale_evidence(change):
    manifest = source()
    frame = manifest["segments"][0]["frames"][0]
    if change == "foreign":
        frame["uri"] = "/media/other/frames/f0.jpg"
    if change == "path":
        frame["file"] = "../outside.jpg"
    if change == "timestamp":
        frame["t"] = float("nan")
    if change == "conflicting":
        alternate = copy.deepcopy(frame)
        alternate["t"] = 1.0
        manifest["segments"][0]["frames"].append(alternate)
    if change == "generation":
        manifest["generation"] = 99
    with pytest.raises(ValueError):
        compile_index("vid_fixture", 2, 40, manifest)


@pytest.mark.parametrize("change", ["foreign", "duplicate", "no_match", "missing_criteria"])
def test_model_output_validation(change):
    frames = source()["segments"][0]["frames"][:1]
    response = payload(frames)
    if change == "foreign":
        response["matches"][0]["frame_id"] = "other"
    if change == "duplicate":
        response["matches"].append(copy.deepcopy(response["matches"][0]))
    if change == "no_match":
        response["no_match"] = True
    if change == "missing_criteria":
        response["criteria"] = []
    with pytest.raises(SearchError):
        checked_matches(response, frames, 40)


def test_long_chinese_context_remains_within_the_bounded_provider_text_envelope():
    context = {
        "question": "之前的问题" * 1000,
        "events": [
            {"summary": "证据描述" * 1000, "evidence": [{"frame_id": "f" * 128, "t": 326.1}] * 8}
        ]
        * 4,
    }
    assert len(verification_prompt("问题" * 2000, context).encode()) < 17000


def test_full_caption_search_paginates_all_hits_beyond_eight_with_snapshot():
    from frameseek.visual_search import retrieve_page

    index = compile_index("vid_fixture", 2, 1000, source(["yellow parcel"] * 95))
    first = retrieve_page(index, "yellow parcel", limit=40)
    second = retrieve_page(index, "yellow parcel", 40, 40, first["snapshot"])
    third = retrieve_page(index, "yellow parcel", 80, 40, first["snapshot"])
    assert first["total_candidates"] == 95
    assert len(first["frames"] + second["frames"] + third["frames"]) == 95
    assert third["next_offset"] is None
    assert third["frames"][-1]["t"] == 940
    assert not third["all_source_occurrences_verified"]
    with pytest.raises(SearchError, match="snapshot"):
        retrieve_page(index, "yellow parcel", 40)
    with pytest.raises(SearchError, match="snapshot"):
        retrieve_page(index, "different question", 40, snapshot=first["snapshot"])
