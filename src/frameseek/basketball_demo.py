"""Public synthetic basketball fixture; it never invokes a model."""

from frameseek.schema import Answer, hms


def catalog() -> dict:
    scenes = {
        "court": (
            0,
            5,
            0,
            (
                "A player in a charcoal jersey holds the ball on the right; "
                "a white-jersey defender is toward the center."
            ),
        ),
        "pass": (
            5,
            11,
            6,
            (
                "The ball is airborne between two players in charcoal "
                "jerseys, with white-jersey defenders nearby."
            ),
        ),
        "shot": (
            11,
            18,
            13,
            "A white-jersey defender faces the shooter in the foreground with one arm raised.",
        ),
    }

    def event(scene):
        start, end, frame, caption = scenes[scene]
        return {
            "video_id": "demo_basketball",
            "start": float(start),
            "end": float(end),
            "start_hms": hms(start),
            "end_hms": hms(end),
            "action": caption,
            "summary": caption,
            "support_level": "high",
            "evidence": [
                {
                    "t": float(frame),
                    "t_hms": hms(frame),
                    "frame_id": f"demo_basketball_f{frame}",
                    "uri": f"/media/demo_basketball/frames/{frame}.jpg",
                    "caption": caption,
                    "asr": None,
                }
            ],
        }

    definitions = [
        (
            "court",
            "Where is the ball handler in the court scene?",
            "ok",
            scenes["court"][3],
            ["court"],
            [],
            {"What is visible in the pass scene?": "pass"},
        ),
        (
            "pass",
            "What is visible in the pass scene?",
            "ok",
            scenes["pass"][3],
            ["pass"],
            [],
            {
                "Where is the defender in the shot scene?": "shot",
                "Compare the court and pass scenes.": "compare",
            },
        ),
        (
            "shot",
            "Where is the defender in the shot scene?",
            "ok",
            scenes["shot"][3],
            ["shot"],
            [],
            {
                "Does the shot score?": "outcome",
                "Compare the pass and shot scenes.": "compare_shot",
            },
        ),
        (
            "compare",
            "Compare the court and pass scenes.",
            "partial",
            f"Court: {scenes['court'][3]} Pass: {scenes['pass'][3]}",
            ["court", "pass"],
            [
                (
                    "These are separate generated stills; they do not establish "
                    "continuous play or player identity."
                )
            ],
            {},
        ),
        (
            "compare_shot",
            "Compare the pass and shot scenes.",
            "partial",
            f"Pass: {scenes['pass'][3]} Shot: {scenes['shot'][3]}",
            ["pass", "shot"],
            [
                (
                    "These are separate generated stills; they do not establish "
                    "continuous play or player identity."
                )
            ],
            {},
        ),
        (
            "outcome",
            "Does the shot score?",
            "partial",
            "The still shows a player preparing a shot, but does not show whether it scores.",
            ["shot"],
            [
                (
                    "The sequence is made from generated stills and does not "
                    "show a continuous play or shot outcome."
                )
            ],
            {},
        ),
    ]
    queries = []
    for key, question, status, answer, scene_ids, unresolved, followups in definitions:
        response = Answer.model_validate(
            {
                "schema_version": "0.1",
                "status": status,
                "timebase": "video_absolute_seconds",
                "answer": answer,
                "events": [event(scene) for scene in scene_ids],
                "unresolved": unresolved,
                "followups": list(followups),
                "trace": {
                    "visible": {
                        "channels": [],
                        "segments_reviewed": len(scene_ids),
                        "evidence_segments": len(scene_ids),
                    },
                    "debug": None,
                },
                "errors": [],
            }
        ).model_dump()
        queries.append(
            {
                "id": key,
                "question": question,
                "response": response,
                "suggested_replies": list(followups),
                "followup_map": followups,
            }
        )
    return {
        "mode": "fixture",
        "synthetic": True,
        "video": {
            "id": "demo_basketball",
            "title": "Basketball scenes · synthetic demo",
            "duration": 18,
            "uri": "/demo/video?video_id=demo_basketball",
        },
        "queries": queries,
        "scenarios": [
            {
                "id": "basketball",
                "title": "Explore the scenes",
                "description": "Review a pass, a shot, and the limits of the evidence.",
                "focus": "Questions and cited frames",
                "layout": "spread",
                "steps": [
                    {"query_id": "pass", "parent": None},
                    {
                        "query_id": "shot",
                        "question": "Where is the defender in the shot scene?",
                        "parent": 0,
                    },
                    {"query_id": "outcome", "question": "Does the shot score?", "parent": 1},
                ],
            }
        ],
    }
