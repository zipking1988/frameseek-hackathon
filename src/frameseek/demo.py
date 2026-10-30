"""Public synthetic demo catalog, independent of private user videos and models."""

from frameseek.schema import Answer, hms

DEMO_IDS = frozenset({"demo_parcel", "demo_basketball"})


def catalog(video_id: str = "demo_parcel") -> dict:
    if video_id == "demo_basketball":
        from frameseek.basketball_demo import catalog as basketball_catalog

        return basketball_catalog()
    if video_id != "demo_parcel":
        raise ValueError("unknown_demo")
    return parcel_catalog()


def parcel_catalog() -> dict:
    def event(start: int, end: int, action: str, caption: str, frames: list[int]) -> dict:
        return {
            "video_id": "demo_parcel",
            "start": float(start),
            "end": float(end),
            "start_hms": hms(start),
            "end_hms": hms(end),
            "action": action,
            "summary": caption,
            "support_level": "high",
            "evidence": [
                {
                    "t": float(t),
                    "t_hms": hms(t),
                    "frame_id": f"demo_parcel_f{t}",
                    "uri": f"/media/demo_parcel/frames/{t}.jpg",
                    "caption": caption,
                    "asr": None,
                }
                for t in frames
            ],
        }

    left = event(0, 5, "包裹位于工作台左侧", "纸箱位于工作台左侧，旁边可见胶带。", [0])
    scene = event(
        0,
        18,
        "工作台与固定物体",
        "三个合成阶段中，可见工作台、顶部亮着的灯具、左侧胶带及桌面工具。",
        [0, 6, 13],
    )
    tape = event(0, 18, "左侧胶带的位置", "起始与结束阶段，胶带均位于工作台左侧。", [0, 13])
    lighting = event(0, 18, "顶部灯具亮着", "三个合成阶段中，顶部灯具均亮着。", [0, 6, 13])
    center = event(5, 11, "包裹出现在工作台中央", "纸箱位于工作台中央，顶部有浅色封箱带。", [6, 10])
    right = event(
        11, 18, "包裹出现在工作台右侧", "纸箱从中央位置切换至右侧；画面中没有可辨认的搬运者。", [13]
    )
    definitions = [
        (
            "center",
            "包裹什么时候出现在中央？",
            "ok",
            "包裹在 00:05–00:11 出现在工作台中央。",
            [center],
            [],
        ),
        ("right", "什么时候移到右侧？", "ok", "从 00:11 开始，包裹出现在工作台右侧。", [right], []),
        (
            "person",
            "能确认是谁搬动的吗？",
            "partial",
            "能确认包裹在 00:11 切换到右侧，但不能确认是谁搬动的。",
            [right],
            ["演示画面没有人物，无法确定搬运者或搬运方式。"],
        ),
        ("forklift", "有叉车出现吗？", "no_result", "这段 18 秒演示画面中没有叉车。", [], []),
        ("failure", "查看失败样例", "error", "这次查找未完成。你已查看的结果仍会保留。", [], []),
    ]
    definitions.extend(
        [
            ("left", "包裹最初在哪里？", "ok", "在 00:00–00:05，包裹位于工作台左侧。", [left], []),
            (
                "timeline",
                "把包裹的位置变化按时间列出来。",
                "ok",
                (
                    "三个阶段依次是左侧 00:00–00:05、中央 00:05–00:11、右侧 00:11–00:18。"
                    "这是合成画面的切换。"
                ),
                [left, center, right],
                [],
            ),
            (
                "dwell",
                "包裹在哪个位置停留时间最长？",
                "ok",
                "右侧阶段最长：7 秒。中央阶段 6 秒，左侧阶段 5 秒；这是三个合成片段的时长比较。",
                [right, center, left],
                [],
            ),
            (
                "compare",
                "对比开始和结束时包裹的位置。",
                "ok",
                "起始阶段包裹在左侧，结束阶段在右侧。两张证据来自同一个合成视频。",
                [left, right],
                [],
            ),
            (
                "continuous",
                "这些画面能证明包裹连续移动吗？",
                "partial",
                "可以确认三个位置阶段，不能据此确认连续移动。演示由静帧切换组成。",
                [left, center, right],
                ["没有真实的连续搬运动作，不能推断移动路径或方式。"],
            ),
            (
                "objects",
                "视频里有哪些固定物体？",
                "ok",
                "画面中可见工作台、顶部灯具、左侧胶带及桌面工具。包裹的位置随合成阶段变化。",
                [scene],
                [],
            ),
            (
                "tape",
                "胶带的位置有变化吗？",
                "ok",
                "起始与结束阶段，胶带都在工作台左侧。可并排核对 00:00 和 00:13 的画面。",
                [tape],
                [],
            ),
            (
                "lighting",
                "灯光状态有什么变化？",
                "ok",
                "三个合成阶段的顶部灯具均亮着，预设样例中未见灯具关闭。",
                [lighting],
                [],
            ),
            (
                "offscreen",
                "灯亮着能说明现场一定有人吗？",
                "partial",
                "能确认灯具亮着，但仅凭灯光不能确定现场是否有人，或画面外是否有人。",
                [lighting],
                ["画面外区域不可见；亮灯不能作为人员在场的直接证据。"],
            ),
            (
                "facts",
                "目前有哪些可确认的事实？",
                "ok",
                "可以确认包裹分别位于左侧、中央和右侧。现有合成画面不能确认搬运者。",
                [left, center, right],
                [],
            ),
            (
                "recheck",
                "重新查看中央证据。",
                "ok",
                (
                    "中央阶段仍可回看：00:05–00:11，证据帧为 00:06 和 00:10。"
                    "这次载入的是预设样例，没有重新请求模型。"
                ),
                [center],
                [],
            ),
            (
                "label",
                "能读出包裹上的快递单号吗？",
                "partial",
                "能看到箱体有标签，但不能可靠读出单号。合成标签不代表真实物流数据。",
                [center],
                ["没有可核验的真实物流单号，不输出虚构号码。"],
            ),
        ]
    )
    continuations = {
        "center": {"那后来呢？": "right", "谁搬的？": "person"},
        "right": {"谁搬的？": "person", "对比前后": "compare"},
        "person": {"那能确定什么？": "facts"},
        "left": {"那后来呢？": "center"},
        "timeline": {"哪段最久？": "dwell", "最初在哪里？": "left"},
        "dwell": {"最初在哪里？": "left", "对比前后": "compare"},
        "compare": {"中间呢？": "center", "是连续移动吗？": "continuous"},
        "continuous": {"能确定哪些位置？": "facts"},
        "objects": {"胶带呢？": "tape", "灯光呢？": "lighting"},
        "tape": {"可以看完整时间线吗？": "timeline"},
        "lighting": {"能说明一定有人吗？": "offscreen"},
        "offscreen": {"谁搬的？": "person"},
        "facts": {"可以看完整时间线吗？": "timeline", "标签能读出来吗？": "label"},
        "label": {"那能确定什么？": "facts"},
        "forklift": {"看看已有的画面": "objects"},
        "failure": {"重新查看中央证据。": "recheck"},
        "recheck": {"那后来呢？": "right"},
    }
    query_titles = {item[0]: item[1] for item in definitions}
    queries = []
    for identifier, question, status, answer, events, unresolved in definitions:
        data = {
            "schema_version": "0.1",
            "status": status,
            "timebase": "video_absolute_seconds",
            "answer": answer,
            "events": events,
            "unresolved": unresolved,
            "followups": [
                query_titles[target] for target in continuations.get(identifier, {}).values()
            ],
            "trace": {
                "visible": {
                    "channels": ["visual_vector"],
                    "segments_reviewed": 3 if status != "error" else 0,
                    "evidence_segments": len(events),
                },
                "debug": None,
            },
            "errors": [],
        }
        if status == "error":
            data["errors"] = [
                {
                    "code": "demo_failure",
                    "stage": "demo",
                    "message": "这是固定失败样例，用来展示错误与无命中的区别。",
                    "cause": "synthetic_demo_failure",
                    "retryable": False,
                    "suggested_action": "选择其他示例问题继续体验。",
                    "docs_url": "/docs/errors",
                    "request_id": "demo_failure",
                }
            ]
        queries.append(
            {
                "id": identifier,
                "question": question,
                "response": Answer.model_validate(data).model_dump(),
                "suggested_replies": list(continuations.get(identifier, {})),
                "followup_map": continuations.get(identifier, {}),
            }
        )
    return {
        "mode": "fixture",
        "synthetic": True,
        "video": {
            "id": "demo_parcel",
            "title": "包裹移动 · 合成演示",
            "duration": 18,
            "uri": "/demo/video",
        },
        "queries": queries,
        "scenarios": [
            {
                "id": "tracking",
                "title": "位置追踪",
                "description": "找到关键阶段，继续追问去向与搬运者。",
                "focus": "连续三轮对话",
                "layout": "spread",
                "steps": [
                    {"query_id": "center", "parent": None},
                    {"query_id": "right", "question": "那后来呢？", "parent": 0},
                    {"query_id": "person", "question": "谁搬的？", "parent": 1},
                ],
            },
            {
                "id": "chronology",
                "title": "完整时间线",
                "description": "一次整理三个阶段，再比较时长和起始位置。",
                "focus": "多段证据与时间排序",
                "layout": "spread",
                "steps": [
                    {"query_id": "timeline", "parent": None},
                    {"query_id": "dwell", "question": "哪段最久？", "parent": 0},
                    {"query_id": "left", "question": "最初在哪里？", "parent": 0},
                ],
            },
            {
                "id": "comparison",
                "title": "前后对比",
                "description": "对照开始和结束，核查中间阶段与证据边界。",
                "focus": "两张证据对照",
                "layout": "spread",
                "steps": [
                    {"query_id": "compare", "parent": None},
                    {"query_id": "center", "question": "中间呢？", "parent": 0},
                    {"query_id": "continuous", "question": "是连续移动吗？", "parent": 0},
                ],
            },
            {
                "id": "branches",
                "title": "多主题分支",
                "description": "从同一个问题，分别调查胶带与灯光。",
                "focus": "选择旧卡片继续问",
                "layout": "spread",
                "steps": [
                    {"query_id": "objects", "parent": None},
                    {"query_id": "tape", "question": "胶带呢？", "parent": 0},
                    {"query_id": "lighting", "question": "灯光呢？", "parent": 0},
                ],
            },
            {
                "id": "verification",
                "title": "反向核查",
                "description": "先问未知信息，再把可确认的事实和标签分开。",
                "focus": "未知项与可确认事实",
                "layout": "spread",
                "steps": [
                    {"query_id": "person", "parent": None},
                    {"query_id": "facts", "question": "那能确定什么？", "parent": 0},
                    {"query_id": "label", "question": "标签能读出来吗？", "parent": 1},
                ],
            },
            {
                "id": "boundaries",
                "title": "无结果与恢复",
                "description": "区分未找到、查找失败与可以继续回看的结果。",
                "focus": "无命中／失败／恢复",
                "layout": "status",
                "steps": [
                    {"query_id": "forklift", "parent": None},
                    {"query_id": "failure", "parent": None},
                    {"query_id": "recheck", "parent": 1},
                ],
            },
        ],
    }
