"use strict";
const $ = (id) => document.getElementById(id);
const player = $("player");
let demo = null;
let activeVideo = null;
let realBusy = false;
let localUrl = null;
let localMode = false;
let mediaReady = false;
let mediaFailed = false;
let pendingSeek = null;
let pendingSeekRestore = false;
let pendingSeekTrigger = null;
let locatingSeek = null;
let turns = 0;
let taskSequence = 0;
let lastQueryId = null;
let evidenceReturnFocus = null;
let investigation = null;
const panelWidths = {source: null, inspector: null};
function restorePanelWidths(saved = {}) {
  const shell = document.querySelector(".agent-shell");
  for (const [side, key] of [["source", "sourceWidth"], ["inspector", "inspectorWidth"]]) {
    const value = saved[key];
    panelWidths[side] = Number.isFinite(value) ? Math.max(side === "source" ? 280 : 260, Math.min(side === "source" ? 900 : 600, value)) : null;
    if (panelWidths[side] === null) shell.style.removeProperty?.(`--${side}-user-width`);
    else shell.style.setProperty?.(`--${side}-user-width`, `${panelWidths[side]}px`);
  }
  for (const side of ["source", "inspector"]) {
    const otherSide = side === "source" ? "inspector" : "source";
    const other = $(side === "source" ? "workbench-inspector" : "source-dock");
    if (panelWidths[side] !== null) {
      panelWidths[side] = window.FrameSeekWorkbenchModel.panelWidth(panelWidths[side], side, window.innerWidth,
        shell.classList.contains(`${otherSide}-collapsed`) ? 0 : other.getBoundingClientRect().width);
      shell.style.setProperty(`--${side}-user-width`, `${panelWidths[side]}px`);
    }
    const actual = panelWidths[side] ?? $(side === "source" ? "source-dock" : "workbench-inspector").getBoundingClientRect?.().width;
    if (Number.isFinite(actual)) document.querySelector(`.${side}-resizer`)?.setAttribute("aria-valuenow", String(Math.round(actual)));
  }
}
function setupPanelResize() {
  if (!window.FrameSeekWorkbenchModel?.panelWidth) return;
  const shell = document.querySelector(".agent-shell");
  for (const side of ["source", "inspector"]) {
    const handle = node("div", `panel-resizer ${side}-resizer`);
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", side === "source" ? "调整素材面板宽度" : "调整检查器宽度");
    handle.title = "拖动调整宽度 · 方向键微调 · 双击恢复默认";
    const panel = $(side === "source" ? "source-dock" : "workbench-inspector");
    const width = () => panel.getBoundingClientRect().width;
    handle.setAttribute("aria-valuemin", side === "source" ? "280" : "260");
    handle.setAttribute("aria-valuemax", side === "source" ? "900" : "600");
    handle.setAttribute("aria-valuenow", String(Math.round(width())));
    const apply = value => {
      const other = $(side === "source" ? "workbench-inspector" : "source-dock");
      const hidden = side === "source" ? shell.classList.contains("inspector-collapsed") : shell.classList.contains("source-collapsed");
      const next = window.FrameSeekWorkbenchModel.panelWidth(value, side, window.innerWidth, hidden ? 0 : other.getBoundingClientRect().width);
      panelWidths[side] = next;
      shell.style.setProperty(`--${side}-user-width`, `${next}px`);
      handle.setAttribute("aria-valuenow", String(next));
      handle.setAttribute("aria-valuetext", `${next} 像素`);
    };
    let drag = null;
    handle.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      e.preventDefault();
      handle.focus();
      drag = {x: e.clientX, width: width(), id: e.pointerId};
      handle.setPointerCapture(e.pointerId);
      shell.classList.add("resizing-panels");
    });
    handle.addEventListener("pointermove", e => {
      if (drag && drag.id === e.pointerId) apply(drag.width + (e.clientX - drag.x) * (side === "source" ? 1 : -1));
    });
    const finish = () => {
      if (!drag) return;
      drag = null; shell.classList.remove("resizing-panels");
      window.dispatchEvent(new Event("frameseek-canvas-changed"));
    };
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
    handle.addEventListener("lostpointercapture", finish);
    handle.addEventListener("keydown", e => {
      if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      e.preventDefault();
      apply(width() + (e.key === "ArrowRight" ? 16 : -16) * (side === "source" ? 1 : -1));
      window.dispatchEvent(new Event("frameseek-canvas-changed"));
    });
    handle.addEventListener("dblclick", () => {
      panelWidths[side] = null; shell.style.removeProperty(`--${side}-user-width`);
      window.dispatchEvent(new Event("frameseek-canvas-changed"));
    });
    shell.append(handle);
    window.addEventListener("resize", () => {if (panelWidths[side] !== null) apply(panelWidths[side]);});
  }
}


function setSourceOpen(open) {
  const wasOpen = !document.querySelector(".agent-shell").classList.contains("source-collapsed");
  document
    .querySelector(".agent-shell")
    .classList.toggle("source-collapsed", !open);
  $("rail-source").setAttribute("aria-expanded", String(open));
  $("rail-source").setAttribute("aria-label", open ? "收起视频面板" : "打开视频面板");
  $("rail-source").title = open ? "收起视频面板" : "展开视频面板";
  $("rail-video").setAttribute("aria-expanded", String(open));
  if (wasOpen !== open) window.dispatchEvent(new Event("frameseek-canvas-changed"));
}

function showEvidence(open, trigger = document.activeElement) {
  if (window.FrameSeekWorkbench) {
    $("evidence-canvas").hidden = false;
    if (open) {
      setSourceOpen(true);
      $("source-dock").scrollTop = 0;
    }
    $("source-dock").classList.toggle("evidence-focused", open);
    return;
  }
  if (investigation) {
    const viewer = $("evidence-canvas");
    const wasOpen = !viewer.hidden;
    if (open && trigger?.isConnected) evidenceReturnFocus = trigger;
    viewer.hidden = !open;
    $("toggle-evidence").setAttribute("aria-expanded", String(open));
    if (!open && wasOpen && evidenceReturnFocus?.isConnected)
      evidenceReturnFocus.focus({ preventScroll: true });
    return;
  }
  const shell = document.querySelector(".agent-shell");
  const wasOpen = shell.classList.contains("evidence-open");
  const mobile = window.innerWidth < 900;
  if (mobile && open && !wasOpen) evidenceReturnFocus = trigger;
  shell.classList.toggle("evidence-open", open);
  $("toggle-evidence").setAttribute("aria-expanded", String(open));
  $("toggle-evidence").firstChild.textContent = open
    ? "返回对话 "
    : "查看视频 ";
  if (mobile && open && !wasOpen) {
    $("toggle-evidence").focus({ preventScroll: true });
  } else if (mobile && !open && wasOpen) {
    const target = evidenceReturnFocus;
    evidenceReturnFocus = null;
    if (target?.isConnected !== false && target?.focus && !target.disabled) {
      target.focus({ preventScroll: true });
    } else {
      (localMode ? $("toggle-evidence") : $("question")).focus({
        preventScroll: true,
      });
    }
  }
}
function resetContext() {
  $("run-state").textContent = localMode ? "未建立索引" : "等待任务";
  updateMediaContext();
  $("result-step").textContent = localMode
    ? "尚未接入视频检索"
    : "从固定样例中匹配";
  $("result-step-state").textContent = localMode ? "不可用" : "待匹配";
  $("evidence-step").textContent = localMode
    ? "此文件没有检索证据"
    : "时间与画面关联";
  $("evidence-step-state").textContent = localMode ? "不可用" : "待选择";
  document.querySelectorAll(".run-steps li").forEach((item, index) => {
    if (index > 0) item.classList.remove("complete");
  });
}

function knowledgeLabel(video) {
  const status = video?.knowledge_status;
  if (status?.state === "ready" && status.version) return "基础索引已就绪";
  if (status?.state === "indexing") return `正在建立基础索引 ${status.completed_segments}/${status.total_segments}`;
  if (status?.state === "waiting_probe") return "等待视频预检";
  if (status?.state === "waiting_processor") return "等待索引配置";
  if (status?.state === "waiting_budget") return "等待预算或授权";
  if (status?.state === "failed" || status?.state === "unknown") return "基础索引未完成";
  return "基础索引尚未建立";
}

function updateKnowledge(video) {
  const status = video?.knowledge_status;
  const control = $("knowledge-control");
  $("video-state").textContent = knowledgeLabel(video);
  control.hidden = !status?.required_for_chat || status.state === "ready";
  control.disabled = status?.state === "waiting_probe" || status?.state === "unknown";
  control.textContent = ["queued", "indexing"].includes(status?.state) ? "停止索引" : "建立基础索引";
}

function updateMediaContext() {
  if (activeVideo) {
    const ready = activeVideo.job?.state === "ready" || Number(activeVideo.metadata?.duration) > 0;
    $("attached-source-state").textContent = mediaFailed ? "播放失败" : ready ? "视频已就绪" : "已上传 · 等待预检";
    $("source-step").textContent = activeVideo.name;
    $("source-step-state").textContent = ready ? "已就绪" : "未就绪";
    return;
  }
  $("source-step").textContent = mediaFailed
    ? "视频加载失败"
    : !mediaReady
      ? "正在加载视频"
      : localMode
        ? "仅浏览器内播放"
        : "合成视频已加载";
  $("source-step-state").textContent = mediaFailed
    ? "需重试"
    : !mediaReady
      ? "加载中"
      : localMode
        ? "未索引"
        : "已就绪";
  $("attached-source-state").textContent = mediaFailed
    ? "加载失败"
    : !mediaReady
      ? "正在加载"
      : localMode
        ? "仅本地播放 · 未索引"
        : "已加载 · 18 秒";
  document
    .querySelector(".run-steps li:first-child")
    .classList.toggle("complete", mediaReady && !mediaFailed && !localMode);
}
const stateLabels = {
  ok: "已提供证据",
  partial: "部分确认",
  no_result: "未找到相关画面",
  error: "查找未完成",
};
function selectTask(data, historyButton, query=null) {
  window.FrameSeekWorkbench?.select(
    historyButton.dataset.sourceTurn || historyButton.dataset.turn,
  );
  document.querySelectorAll(".history-task").forEach((item) => {
    const active = item === historyButton;
    item.classList.toggle("active", active);
    item.setAttribute("aria-current", active ? "true" : "false");
  });
  document.querySelectorAll(".turn").forEach((item) => {
    item.classList.toggle("selected", item.id === historyButton.dataset.turn);
  });
  $("run-state").textContent = query?.engine==='index' ? "索引检索已完成" : stateLabels[data.status];
  $("result-step").textContent = query?.engine==='index' ? "读取已有索引 · 未调用模型" : activeVideo ? "真实模型检索与回看" : "已匹配固定演示样例";
  $("result-step-state").textContent = "已匹配";
  document
    .querySelector(".run-steps li:nth-child(2)")
    .classList.add("complete");
  $("evidence-step").textContent = data.events.length
    ? `${data.events.length} 个事件可回看`
    : query?.engine==='index' ? "可在索引时间线回看描述候选" : "该样例没有可定位的事件";
  $("evidence-step-state").textContent = data.events.length
    ? "待选择"
    : "无证据";
  document
    .querySelector(".run-steps li:last-child")
    .classList.remove("complete");
}

const clock = (seconds) => {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
};
function announce(message) {
  $("announcement").textContent = message;
}
function notice(message) {
  $("form-notice").textContent = message;
  $("form-notice").hidden = !message;
}
function preflightFeedback(message) {
  $("question-feedback-message").textContent = message;
  if (!$("question-feedback-dialog").open) $("question-feedback-dialog").showModal();
}
$("question-feedback-edit").addEventListener("click", () => $("question-feedback-dialog").close());
$("question-feedback-dialog").addEventListener("close", () => $("question").focus());
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function button(label, className, action) {
  const element = node("button", className, label);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}
function updateSend() {
  $("send-question").disabled =
    !demo || localMode || realBusy || !$("question").value.trim();
}
function seek(seconds, trigger = document.activeElement) {
  if (!Number.isFinite(seconds) || seconds < 0 || (mediaReady && seconds > player.duration)) return;
  if (mediaFailed) {
    announce("视频无法定位，请先重新加载视频。");
    $("seek-status").textContent = "视频无法定位，请先重新加载视频。";
    return;
  }
  if (!mediaReady) {
    pendingSeek = seconds;
    pendingSeekTrigger = trigger;
    announce("视频仍在加载，加载后将定位到对应画面。");
    return;
  }
  const wasPaused = player.paused;
  locatingSeek = seconds;
  $("seek-status").textContent = `正在定位 ${clock(seconds)} · 等待画面加载…`;
  announce($("seek-status").textContent);
  player.currentTime = seconds;
  window.dispatchEvent(new Event("frameseek-explicit-seek"));
  if (wasPaused) player.pause();
  if (investigation) {
    if (!trigger?.closest?.(".turn")) investigation.evidence(null);
    showEvidence(true, trigger);
  }
  if (!investigation && window.innerWidth < 900) {
    showEvidence(true, trigger);
    player.scrollIntoView({
      block: "center",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }
  $("evidence-step").textContent = `正在定位 ${clock(seconds)}`;
  $("evidence-step-state").textContent = "加载中";
  document.querySelector(".run-steps li:last-child").classList.remove("complete");
  completeSeek();
}
function completeSeek() {
  if (locatingSeek === null || player.seeking || player.readyState < 2 || Math.abs(player.currentTime - locatingSeek) > 0.25) return;
  const seconds = locatingSeek;
  locatingSeek = null;
  $("seek-status").textContent = `已定位 ${clock(seconds)} · ${player.paused ? "保持暂停" : "继续播放"}`;
  $("evidence-step").textContent = `已定位到 ${clock(seconds)}`;
  $("evidence-step-state").textContent = "已定位";
  document.querySelector(".run-steps li:last-child").classList.add("complete");
  announce($("seek-status").textContent);
}
function updateTime() {
  if (investigation && mediaReady && locatingSeek === null)
    $("seek-status").textContent =
      `当前 ${clock(player.currentTime)} · ${player.paused ? "已暂停" : "播放中"}`;
  $("playback-time").textContent =
    `${clock(player.currentTime)} / ${clock(player.duration)}`;
  const percent = player.duration
    ? (player.currentTime / player.duration) * 100
    : 0;
  $("playhead").style.transform = `scaleX(${Math.min(percent, 100) / 100})`;
  const start = player.currentTime < 5 ? 0 : player.currentTime < 11 ? 5 : 11;
  document.querySelectorAll(".chapter,.segment").forEach((element) => {
    const activeTime = element.classList.contains("segment")
      ? start === 5
        ? 6
        : start === 11
          ? 13
          : 0
      : start;
    const active = !localMode && Number(element.dataset.time) === activeTime;
    element.classList.toggle("active", active);
    element.setAttribute("aria-current", active ? "time" : "false");
  });
}
function renderAnswer(query) {
  if ((activeVideo && (query.mode !== "real" || query.video_id !== activeVideo.id)) || (!activeVideo && query.mode === "real")) return;
  if (localMode || !demo) return;
  if (!query.savedTurn && window.FrameSeekWorkbench?.getSnapshot().records.length >= 100) {
    announce("当前调查的保存空间已满，原聊天没有删除；请建立另一份调查。");
    return;
  }
  if (++turns > 12 && !window.FrameSeekWorkbench) {
    const oldestId = $("messages").firstElementChild?.id;
    if (oldestId) document.querySelector(`[data-turn="${oldestId}"]`)?.remove();
    $("messages").firstElementChild?.remove();
  }
  const data = query.response;
  $("suggestions").hidden = true;
  const more = node("details", "response-details");
  more.append(node("summary", "", "更多证据与引用"));
  const turn = node("article", "turn question-node");
  turn.id = query.savedTurn || `task-${++taskSequence}`;
  taskSequence = Math.max(taskSequence, Number(turn.id.split("-")[1]) || 0);
  turn.dataset.kind = "question";
  turn.append(node("p", "question-bubble", query.question));
  const label = node("div", "answer-label", "FrameSeek");
  label.append(
    node(
      "span",
      "fixture-tag",
      query.engine === "index" ? "索引问答 · 未调用模型" : query.mode === "real" ? (query.usedContext ? "证据追问 · 承接前文" : "真实分析") : query.usedContext ? "预设追问 · 承接前文" : "预设演示 · 未调用模型",
    ),
  );
  turn.append(label, node("p", "answer-text", data.answer));
  turn.append(
    node("span", `answer-status ${data.status}`, query.engine === "index" ? (data.events.length ? "已有观察匹配" : "索引结果 · 覆盖待补齐") : stateLabels[data.status]),
  );
  if(query.engine==='index') turn.append(button('查看索引时间线 · 不调用模型','outline-button',()=>window.FrameSeekPersistence?.searchIndex({question:query.index_search?.resolved_question || query.question,answerRequestId:query.id})));
  const eventCluster =
    data.events.length > 1 ? node("div", "event-cluster") : turn;
  if (eventCluster !== turn) turn.append(eventCluster);
  const previewEvents = ["local", "cloud", "index"].includes(query.engine)
    ? data.events.slice(0, 1) : data.events;
  previewEvents.forEach((event) => {
    const row = node("section", "event");
    row.append(node("h3", "event-heading", event.action));
    const toolbar = node("div", "event-toolbar");
    toolbar.append(
      button(
        `${clock(event.start)}–${clock(event.end)}`,
        "seek-button",
        (clickEvent) => {
          investigation?.evidence(query);
          seek(event.start, clickEvent.currentTarget);
        },
      ),
      node("span", "support-label", query.mode === "real" ? "画面证据 · 需核实边界" : "固定样例 · 画面直接支持"),
    );
    row.append(toolbar);
    const evidence = node("div", "evidence-grid");
    event.evidence.forEach((frame, index) => {
      const frameButton = button("", "evidence-button", () => {
        investigation?.evidence(query);
        document.querySelectorAll(".evidence-button").forEach((item) => {
          const selected = item === frameButton;
          item.classList.toggle("selected", selected);
          item.setAttribute("aria-pressed", String(selected));
        });
        seek(frame.t, frameButton);
      });
      frameButton.setAttribute("aria-pressed", "false");
      frameButton.setAttribute("aria-label", `定位证据帧 ${clock(frame.t)}`);
      const image = document.createElement("img");
      image.src = frame.uri;
      image.alt = frame.caption;
      image.loading = "lazy";
      const caption = node("span", "evidence-caption", clock(frame.t));
      caption.append(node("span", "", "查看此帧 ↗"));
      frameButton.append(image, caption);
      if (
        index === 0 ||
        ["objects", "tape", "lighting", "offscreen"].includes(query.id)
      )
        evidence.append(frameButton);
      else more.append(frameButton);
    });
    row.append(evidence, node("p", "event-summary", event.summary));
    more.append(
      button("复制时间引用", "text-button", async (eventClick) => {
        const trigger = eventClick.currentTarget;
        try {
          await navigator.clipboard.writeText(
            `${query.mode === "real" ? activeVideo?.name || query.video_id : "合成演示："+demo.video.title} ${clock(event.start)}–${clock(event.end)} — ${event.action}`,
          );
          trigger.textContent = "已复制";
          announce("已复制当前素材的时间引用。");
        } catch {
          notice("浏览器不允许复制。可在结构化结果中选择并复制时间。 ");
        }
      }),
    );
    eventCluster.append(row);
  });
  if (previewEvents.length < data.events.length) {
    const count = data.events.reduce((total, event) => total + event.evidence.length, 0);
    turn.append(button(`查看全部 ${count} 张引用`, "outline-button", () => {
      selectTask(data, historyButton, query);
      investigation?.choose(turn.id);
      openInspector("context");
    }));
  }
  if (data.unresolved.length) {
    const unresolved = node("div", "unresolved");
    unresolved.append(node("strong", "", "仍未确认"));
    data.unresolved.forEach((text) => unresolved.append(node("p", "", text)));
    (investigation ? more : turn).append(unresolved);
  }
  if (data.errors.length) {
    turn.append(
      node("p", "trace-note", data.errors[0].message),
      button(query.mode === "real" ? "编辑问题重新提问" : "选择其他示例问题", "text-button", () => {
        if (query.mode === "real") window.FrameSeekUI.continueFrom(turn.id);
        else {
          $("question").focus();
          notice("可以尝试：包裹什么时候出现在中央？");
        }
      }),
    );
  } else {
    turn.append(
      node(
        "p",
        "trace-note",
        query.engine==='index' ? "已检索已有记录 · 未新增视觉复核" : query.mode === "real" ? `检索与回看 · ${data.trace.visible.segments_reviewed} 个片段` : "演示检查范围：00:00–00:18 · 无音轨 · 非真实模型评测",
      ),
    );
  }
  const details = node("details", "response-details");
  details.append(
    node("summary", "", "查看结构化结果"),
    node("pre", "", JSON.stringify(data, null, 2)),
  );
  more.append(details);
  const actions = node("div", "response-actions");
  data.followups.forEach((question) =>
    actions.append(
      button(question, "text-button", () => {
        fillSuggestedQuestion(question, turn.id);
      }),
    ),
  );
  more.append(actions);
  turn.append(more);
  $("empty-state").hidden = true;
  $("messages").append(turn);
  const historyButton = button(query.question, "history-task", () => {
    if (!investigation)
      turn.scrollIntoView({
        block: "start",
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
      });
    selectTask(data, historyButton, query);
  });
  historyButton.dataset.turn = turn.id;
  historyButton.title = query.question;
  $("task-list").prepend(historyButton);
  $("history-empty").hidden = true;
  $("task-count").textContent = $("messages").children.length;
  selectTask(data, historyButton, query);
  const chatContent = $("chat-content");
  if (!investigation) chatContent.scrollTop = chatContent.scrollHeight;
  window.FrameSeekWorkbench?.record(turn.id, query);
  if (window.FrameSeekWorkbench) {
    const retained = new Set(window.FrameSeekWorkbench.getSnapshot().records.map(r => r.id));
    for (const old of [...$("messages").children]) if (!retained.has(old.id)) {
      investigation?.remove(old.id);
      document.querySelector(`[data-turn="${old.id}"]`)?.remove();
      old.remove();
    }
    $("task-count").textContent = $("messages").children.length;
  }
  investigation?.register(turn, query, historyButton);
  announce(`${query.mode === "real" ? "回答" : "演示回答"}：${query.engine==='index' ? "索引检索完成" : stateLabels[data.status]}。${data.answer}`);
}
function fillSuggestedQuestion(question, turnId = null) {
  const input = $("question");
  if (input.value.trim()) {
    notice("已有草稿，未覆盖。可先编辑或清空输入，再选择建议问题。");
    input.focus(); return false;
  }
  if (input.disabled || realBusy) return false;
  input.value = question;
  if (turnId) investigation?.choose(turnId);
  updateSend();
  if (input.dispatchEvent) input.dispatchEvent(new Event("input", {bubbles:true}));
  input.focus(); return true;
}
function ask(question) {
  if (localMode || realBusy) return;
  if (activeVideo) { window.FrameSeekPersistence?.ask(question); return; }
  const scoped = investigation?.context();
  const contextId = investigation ? scoped?.query.id || null : lastQueryId;
  const normalized = (value) => value.replace(/[\s？?。！!]/g, "");
  const resolved = window.FrameSeekDemoConversation
    ? window.FrameSeekDemoConversation.resolve(
        question,
        demo?.queries || [],
        contextId,
      )
    : {
        query: demo?.queries.find(
          (item) => normalized(item.question) === normalized(question),
        ),
        usedContext: false,
      };
  if (!resolved.query) {
    const parcelContext = ["center", "right", "person"].includes(contextId);
    const supported = scoped?.query.suggested_replies || [];
    notice(
      supported.length
        ? `当前话题支持${supported.map((label) => `「${label}」`).join("、")}。输入已保留。`
        : parcelContext
          ? "这个追问暂未接入。演示支持「那后来呢？」「谁搬的？」或完整示例问题；输入已保留。"
          : contextId
            ? "当前话题不支持这个追问。请改问完整示例问题，例如「包裹什么时候出现在中央？」。输入已保留。"
            : "先从完整示例问题开始，例如「包裹什么时候出现在中央？」。回答后可继续追问。输入已保留。",
    );
    announce("此问题不属于演示样例，未调用模型。");
    return;
  }
  notice("");
  lastQueryId = resolved.query.id;
  renderAnswer({
    ...resolved.query,
    question,
    usedContext: resolved.usedContext,
    parentTurn: scoped?.turn.id || null,
  });
  $("question").value = "";
  updateSend();
  $("question").focus();
}
function clearChat() {
  evidenceReturnFocus = null;
  investigation?.clear();
  window.FrameSeekWorkbench?.clear();
  $("board-artifacts").replaceChildren();
  $("messages").replaceChildren();
  $("empty-state").hidden = false;
  $("question").value = "";
  turns = 0;
  taskSequence = 0;
  $("demo-current").hidden = true;
  lastQueryId = null;
  $("task-list").replaceChildren();
  $("task-count").textContent = "0";
  $("history-empty").hidden = false;
  $("suggestions").hidden = localMode;
  resetContext();
  notice("");
  updateSend();
}
function openDemoLibrary() {
  if (demo?.scenarios?.length) $("demo-library").showModal();
}
async function startScenario(id) {
  if (activeVideo) { await window.FrameSeekPersistence?.openDemo(); if (activeVideo) return; }
  const scenario = demo?.scenarios?.find((item) => item.id === id);
  let seeds;
  try {
    seeds = window.FrameSeekDemoConversation.materialize(
      scenario,
      demo.queries,
    );
  } catch {
    announce("这条演示无法载入，请选择其他调查路径。");
    return;
  }
  if (localMode) $("restore-demo").click();
  $("demo-library").close();
  player.pause();
  locatingSeek = null;
  pendingSeek = null;
  pendingSeekTrigger = null;
  if (mediaReady) player.currentTime = 0;
  showEvidence(false);
  clearChat();
  const turnIds = [];
  for (const seed of seeds) {
    renderAnswer({
      ...seed,
      parentTurn: seed.parentIndex === null ? null : turnIds[seed.parentIndex],
    });
    turnIds.push(`task-${taskSequence}`);
    lastQueryId = seed.id;
  }
  investigation?.arrange(scenario.layout || "threads");
  $("demo-current").textContent = `${scenario.title} · ${seeds.length} 轮预设`;
  $("demo-current").hidden = false;
  window.FrameSeekWorkbench?.rename(`${scenario.title}调查`);
  if (window.FrameSeekWorkbench && window.innerWidth < 1400) {
    $("zoom-reset").click();
    investigation.choose(turnIds[0]);
    investigation.reveal(turnIds[0]);
  }
  $("question").focus();
  announce(
    `已载入${scenario.title}，${seeds.length}轮预设演示。可选择卡片继续追问。`,
  );
}
function renderScenarios() {
  if (!demo.scenarios?.length) return;
  const list = $("demo-scenarios");
  list.replaceChildren();
  demo.scenarios.forEach((scenario, index) => {
    const entry = button("", "demo-scenario", () => startScenario(scenario.id));
    entry.setAttribute("aria-label", `载入演示：${scenario.title}`);
    entry.append(
      node("span", "demo-scenario-number", String(index + 1).padStart(2, "0")),
      node("strong", "", scenario.title),
      node("p", "", scenario.description),
      node(
        "span",
        "demo-scenario-focus",
        `${scenario.focus} · ${scenario.steps.length} 轮`,
      ),
    );
    list.append(entry);
  });
  $("open-demos").disabled = false;
  $("demo-library-empty").disabled = false;
}
$("open-demos").addEventListener("click", openDemoLibrary);
$("demo-library-empty").addEventListener("click", openDemoLibrary);
$("close-demos").addEventListener("click", () => $("demo-library").close());
function setLocalMode(enabled) {
  localMode = enabled;
  window.FrameSeekWorkbench?.source(enabled);
  $("source-dock").classList.toggle("local-source", enabled);
  investigation?.source(enabled);
  $("mode-label").textContent = enabled ? "本地播放" : "演示模式";
  $("demo-notice-text").textContent = enabled
    ? "本地播放模式。文件只在此浏览器中打开，未上传、未建立索引，也未发送给模型。"
    : "正在体验固定演示。视频为合成素材，回答与上下文追问为预设样例；不会上传文件或调用模型。";
  $("restore-demo").hidden = !enabled;
  $("suggestions").hidden = enabled;
  $("composer-mode-text").textContent = enabled
    ? "仅本地播放 · 未建立索引"
    : "预设演示 · 未调用模型";
  $("demo-timeline").hidden = enabled;
  $("segments").hidden = enabled;
  $("frame-heading").hidden = enabled;
  $("empty-eyebrow").textContent = enabled ? "仅本地播放" : "已加载演示视频";
  $("local-note").hidden = !enabled;
  $("video-watermark").hidden = enabled;
  $("question").disabled = enabled;
  $("question").placeholder = enabled
    ? "当前视频未建立索引，暂时不能提问。"
    : "继续追问，或提出新的问题…";
  document.querySelectorAll("[data-query]").forEach((element) => {
    element.disabled = enabled || !demo;
  });
  $("video-state").textContent = enabled ? "仅本地播放" : "演示素材";
  document.querySelector(".source-item span").textContent = enabled
    ? "本地视频 · 未索引"
    : "包裹移动演示";
  $("attached-source-state").textContent = enabled
    ? "仅本地播放 · 未索引"
    : "已加载 · 18 秒";
  $("empty-state").querySelector(investigation ? "h1" : "h2").textContent =
    enabled
      ? "素材已打开，尚未建立索引。"
      : investigation
        ? "从一个问题，展开调查。"
        : "想找哪个画面？";
  $("empty-state").querySelector(".task-intro > p").textContent = enabled
    ? "你可以在视频工作区中播放文件，或返回演示体验预设对话。"
    : investigation
      ? "找到的画面会留在这里。继续追问，把发现连起来。"
      : "先问包裹的位置，再继续追问。";
  $("video-state").title = enabled ? "未上传、未分析" : "公开合成素材";
  $("seek-status").textContent = enabled
    ? "未上传 · 未分析 · 文件只在浏览器内播放"
    : "点击时间或证据帧，定位到对应画面";
  clearChat();
  if (enabled) {
    notice("已打开本地视频。真实索引和问答尚未接入，可返回演示体验证据跳转。");
  }
}
function inspectorIsOpen() {
  const shell = document.querySelector(".agent-shell");
  return window.innerWidth < 1600
    ? shell.classList.contains("inspector-open")
    : !shell.classList.contains("inspector-collapsed");
}
function restoreInspector(open) {
  const shell = document.querySelector(".agent-shell");
  shell.classList.toggle("inspector-open", open);
  shell.classList.toggle("inspector-collapsed", !open);
  syncInspectorToggle();
}
function toggleInspector() {
  const shell = document.querySelector(".agent-shell");
  if (shell.classList.contains("conversation-mode")) {
    const open = !shell.classList.contains("reading-inspector-open");
    shell.classList.toggle("reading-inspector-open", open);
    restoreInspector(open);
    return;
  }
  restoreInspector(!inspectorIsOpen());
  $("toggle-inspector").setAttribute(
    "aria-expanded",
    String(
      window.innerWidth < 1600
        ? shell.classList.contains("inspector-open")
        : !shell.classList.contains("inspector-collapsed"),
    ),
  );
  if (window.innerWidth < 1600 && shell.classList.contains("inspector-open")) {
    $("zoom-reset").click();
    const current = investigation?.context();
    if (current) investigation.reveal(current.turn.id);
  } else investigation?.fit();
  $("toggle-inspector").focus();
}
function syncInspectorToggle() {
  const shell = document.querySelector(".agent-shell");
  $("toggle-inspector").setAttribute(
    "aria-expanded",
    String(
      window.innerWidth < 1600
        ? shell.classList.contains("inspector-open")
        : !shell.classList.contains("inspector-collapsed"),
    ),
  );
}
syncInspectorToggle();
window.addEventListener("resize", syncInspectorToggle);
$("chat-content").addEventListener("dragover", (event) => {
  if (
    Array.from(event.dataTransfer.types).includes(
      "application/x-frameseek-evidence",
    )
  ) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    $("chat-content").classList.add("drop-ready");
  }
});
$("chat-content").addEventListener("dragleave", () =>
  $("chat-content").classList.remove("drop-ready"),
);
$("chat-content").addEventListener("drop", (event) => {
  $("chat-content").classList.remove("drop-ready");
  const payload = event.dataTransfer.getData(
    "application/x-frameseek-evidence",
  );
  if (!payload) return;
  event.preventDefault();
  try {
    const reference = JSON.parse(payload);
    const record = window.FrameSeekWorkbench?.getSnapshot().records.find(
      (record) => record.id === reference.turnId,
    );
    const frame = record?.query.response.events
      .flatMap((event) => event.evidence)
      .find((frame) => frame.uri === reference.uri && frame.t === reference.t);
    if (frame)
      placeFinding(
        window.FrameSeekWorkbench.pinFrame(record.id, record.query, frame),
      );
  } catch {
    announce("无法保存这个画面，请使用引用画面上的保存按钮。");
  }
});
function placeFinding(pin) {
  if (!pin || !investigation) return null;
  if (document.getElementById(pin.id)) {
    investigation.choose(pin.id);
    investigation.reveal(pin.id);
    return pin;
  }
  const card = node("article", `turn ${pin.kind}-node`);
  card.id = pin.id;
  card.dataset.kind = pin.kind;
  card.append(node("p", "question-bubble", pin.title));
  if (pin.kind === "evidence") {
    const frameButton = button("", "evidence-button", () => {
      investigation.evidence(pin.query);
      seek(pin.frame.t, frameButton);
    });
    frameButton.setAttribute(
      "aria-label",
      `回看画布证据 ${clock(pin.frame.t)}`,
    );
    const image = document.createElement("img");
    image.src = pin.frame.uri;
    image.alt = pin.frame.caption;
    frameButton.append(
      image,
      node("span", "evidence-caption", clock(pin.frame.t)),
    );
    card.append(frameButton, node("p", "artifact-caption", pin.frame.caption));
  } else {
    card.append(node("p", "answer-text", pin.query.response.answer));
    pin.query.response.unresolved.forEach((text) =>
      card.append(node("p", "artifact-unresolved", text)),
    );
  }
  card.append(node("p", "artifact-source", `来源：${pin.query.question}`));
  card.append(
    button("从发现集合移除", "text-button", () => removeFinding(pin.id)),
  );
  $("board-artifacts").append(card);
  const history = node("button", "");
  history.dataset.turn = card.id;
  history.dataset.sourceTurn = pin.turnId;
  investigation.register(
    card,
    {
      ...pin.query,
      question: pin.title,
      kind: pin.kind,
    mode: pin.query.mode,
    video_id: pin.query.video_id,
      sourceTurn: pin.turnId,
      parentTurn: pin.turnId,
    },
    history,
  );
  announce(
    `已保存${pin.kind === "evidence" ? "证据帧" : "结论"}，可在画布移动。`,
  );
  return pin;
}
function removeFinding(id) {
  const source = window.FrameSeekWorkbench?.getSnapshot().pins.find(
    (pin) => pin.id === id,
  )?.turnId;
  document.getElementById(id)?.remove();
  investigation?.remove(id);
  window.FrameSeekWorkbench?.remove(id);
  if (source) investigation?.choose(source);
  announce("已从发现集合移除，可再次保存。");
}
window.FrameSeekUI = {
  select: (id) => {
    investigation?.choose(id);
    if (!document.querySelector(".agent-shell").classList.contains("conversation-mode")) investigation?.reveal(id);
    window.dispatchEvent(new Event("frameseek-thread-focus"));
  },
  seek: (seconds, query) => {
    seek(seconds);
    investigation?.evidence(query);
  },
  saveEvidence: (id, query, frame) =>
    placeFinding(window.FrameSeekWorkbench?.pinFrame(id, query, frame)),
  saveConclusion: (id, query) =>
    placeFinding(window.FrameSeekWorkbench?.pinConclusion(id, query)),
  focusFinding: (id) => {
    investigation?.choose(id);
    investigation?.reveal(id);
  },
  removeFinding,
  toggleInspector,
  demos: openDemoLibrary,
  openInspector,
  toggleSource: () => $("rail-source").click(),
  canvas: () => $("rail-canvas").click(),
  openLocal: () => $("open-local").click(),
  newTask,
  fillSuggestedQuestion,
  hideTurn: (id) => requestTurnDeletion(id),
  restoreTurn: (id) => changeTurnVisibility(id, false),
  continueFrom: (id) => {
    investigation?.choose(id);
    const query = window.FrameSeekWorkbench?.getSnapshot().records.find(record => record.id === id)?.query;
    if (query?.mode === "real" && !query.response.events.length) {
      if (!$("question").value.trim()) {
        $("question").value = query.question;
        $("question").dispatchEvent(new Event("input", {bubbles:true}));
      }
      notice(`${window.FrameSeekWorkbenchModel.queryFailureReason(query)} 原问题已保留，可编辑后自行发送；不会自动重试，发送仍需预算检查。`);
      updateSend();
    }
    $("question").focus();
  },
};
let pendingTurnDeletion = null;
function requestTurnDeletion(id) {
  const record = window.FrameSeekWorkbench?.getSnapshot().records.find(item => item.id === id && !item.hidden);
  if (!record) return;
  pendingTurnDeletion = {id, queryId: record.query.id, trigger: document.activeElement};
  $("delete-turn-question").textContent = record.query.question;
  $("delete-turn-dialog").showModal();
  $("delete-turn-cancel").focus();
}
function cancelTurnDeletion() {
  const trigger = pendingTurnDeletion?.trigger;
  pendingTurnDeletion = null;
  $("delete-turn-dialog").close();
  if (trigger?.isConnected) trigger.focus();
}
$("delete-turn-cancel").addEventListener("click", cancelTurnDeletion);
$("delete-turn-dialog").addEventListener("cancel", event => {event.preventDefault(); cancelTurnDeletion();});
$("delete-turn-dialog").addEventListener("close", () => {pendingTurnDeletion = null;});
$("delete-turn-confirm").addEventListener("click", () => {
  const pending = pendingTurnDeletion;
  const record = window.FrameSeekWorkbench?.getSnapshot().records.find(item => item.id === pending?.id && !item.hidden);
  pendingTurnDeletion = null;
  $("delete-turn-dialog").close();
  if (record && record.query.id === pending.queryId) changeTurnVisibility(pending.id, true);
});
function changeTurnVisibility(id, hidden) {
  const saved = window.FrameSeekSessionUI.snapshot();
  const next = window.FrameSeekWorkbenchModel.toggleTurn(saved, id, hidden);
  if (!next) return;
  window.FrameSeekSessionUI.restore(next, activeVideo || {id: "demo_parcel"});
  notice(hidden ? "问题已从画布删除。可在左侧「已删除的问题」中恢复；已保存证据保留。" : "问题已恢复。输入和画布位置已保留。");
  $("question").focus();
}

function openInspector(tab = "context") {
  document.querySelector(".agent-shell").classList.add("reading-inspector-open");
  window.FrameSeekWorkbench?.showPanel(tab);
  restoreInspector(true);
  investigation?.fit();
  $(
    tab === "findings" ? "inspector-findings-tab" : "inspector-context-tab",
  ).focus();
}
$("rail-findings").addEventListener("click", () => openInspector("findings"));
$("toggle-inspector").addEventListener("click", toggleInspector);
$("collapse-source").addEventListener("click", () => {
  setSourceOpen(false);
  $("rail-source").focus();
});
$("rail-canvas").addEventListener("click", () => { setSourceOpen(true); $("source-dock").scrollTop = 0; });
$("rail-source").addEventListener("click", () =>
  setSourceOpen(
    document
      .querySelector(".agent-shell")
      .classList.contains("source-collapsed"),
  ),
);
setSourceOpen(
  !document
    .querySelector(".agent-shell")
    .classList.contains("source-collapsed"),
);
$("question-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!$("send-question").disabled) ask($("question").value.trim());
});
$("question").addEventListener("input", updateSend);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("question-form").requestSubmit();
  }
});
document.querySelectorAll("[data-query]").forEach((element) =>
  element.addEventListener("click", () => {
    const query = demo?.queries.find(
      (item) => item.id === element.dataset.query,
    );
    if (query) fillSuggestedQuestion(query.question);
  }),
);
document
  .querySelectorAll("[data-time]")
  .forEach((element) =>
    element.addEventListener("click", () => seek(Number(element.dataset.time))),
  );
function newTask() {
  if (window.FrameSeekPersistence) { window.FrameSeekPersistence.newInvestigation(); return; }
  clearChat();
  if (localMode) {
    notice("当前视频仅供本地播放，返回演示后可提问。");
  }
  showEvidence(false);
  if (!localMode) $("question").focus();
  announce("演示对话已重置。");
}
$("reset-chat").addEventListener("click", newTask);
$("mobile-reset").addEventListener("click", newTask);
$("toggle-evidence").addEventListener("click", () => {
  const open = $("toggle-evidence").getAttribute("aria-expanded") !== "true";
  showEvidence(open);
  if (!open) $("question").scrollIntoView({ block: "center" });
});
["open-local", "attach-local"].forEach((id) => {
  $(id).addEventListener("click", () => $("file-input").click());
});
$("rail-video").addEventListener("click", () => {
  investigation?.evidence(null);
  showEvidence(true);
  player.focus();
});
$("file-input").addEventListener("change", () => {
  const file = $("file-input").files[0];
  if (file) window.FrameSeekPersistence?.upload(file);
  $("file-input").value = "";
});
$("restore-demo").addEventListener("click", () => {
  if (activeVideo) { window.FrameSeekPersistence?.openDemo(); return; }
  player.pause();
  if (localUrl) URL.revokeObjectURL(localUrl);
  localUrl = null;
  mediaReady = false;
  mediaFailed = false;
  locatingSeek = null;
  pendingSeek = null;
  player.src = "/demo/video";
  player.preload = "auto";
  player.poster = "/media/demo_parcel/frames/0.jpg";
  player.load();
  setLocalMode(false);
  $("video-heading").textContent = "包裹移动 · 合成演示";
  $("attached-source-name").textContent = "包裹移动演示.mp4";
  $("video-meta").textContent = "18 秒 · 1600 × 900 · 无音轨";
  $("file-input").value = "";
  showEvidence(false);
  announce("已返回固定演示，原本地文件没有上传。");
});
function mediaLoaded() {
  mediaReady = true;
  mediaFailed = false;
  $("reload-video").hidden = true;
  updateMediaContext();
  if (localMode || activeVideo)
    $("video-meta").textContent =
      `${clock(player.duration)} · ${player.videoWidth} × ${player.videoHeight} · ${activeVideo?.job?.state === "ready" || Number(activeVideo?.metadata?.duration) > 0 ? "视频预检完成" : "视频预检未完成"}`;
  if (pendingSeek !== null) {
    const seconds = pendingSeek;
    const trigger = pendingSeekTrigger;
    pendingSeek = null;
    pendingSeekTrigger = null;
    if (pendingSeekRestore) { player.currentTime = seconds; player.pause(); pendingSeekRestore = false; }
    else seek(seconds, trigger);
  }
  updateTime();
}
player.addEventListener("loadedmetadata", mediaLoaded);
if (player.readyState >= 1) mediaLoaded();
else updateMediaContext();
player.addEventListener("error", () => {
  locatingSeek = null;
  mediaReady = false;
  mediaFailed = true;
  updateMediaContext();
  pendingSeek = null;
  $("seek-status").textContent =
    "视频无法播放，请重新加载或选择浏览器支持的格式。";
  $("reload-video").hidden = false;
  announce($("seek-status").textContent);
});
$("reload-video").addEventListener("click", () => {
  locatingSeek = null;
  mediaFailed = false;
  mediaReady = false;
  updateMediaContext();
  player.load();
  $("seek-status").textContent = "正在重新加载视频…";
});
player.addEventListener("timeupdate", updateTime);
player.addEventListener("seeking", () => {
  if (locatingSeek !== null && Math.abs(player.currentTime - locatingSeek) > 0.25) {
    locatingSeek = player.currentTime;
    $("seek-status").textContent = `正在定位 ${clock(locatingSeek)} · 等待画面加载…`;
    $("evidence-step").textContent = `正在定位 ${clock(locatingSeek)}`;
  }
});
player.addEventListener("seeked", completeSeek);
player.addEventListener("loadeddata", completeSeek);
player.addEventListener("play", updateTime);
player.addEventListener("pause", updateTime);
window.addEventListener("beforeunload", () => {
  if (localUrl) URL.revokeObjectURL(localUrl);
});
document.querySelectorAll("[data-query]").forEach((element) => {
  element.disabled = true;
});
if (window.FrameSeekCanvas)
  investigation = window.FrameSeekCanvas.start({
    select: selectTask,
    ask: fillSuggestedQuestion,
    announce,
    close: () => showEvidence(false),
  });
fetch("/demo/catalog")
  .then((response) => {
    if (!response.ok) throw new Error("catalog");
    return response.json();
  })
  .then((data) => {
    demo = data;
    renderScenarios();
    document.querySelectorAll("[data-query]").forEach((element) => {
      element.disabled = localMode;
    });
    updateSend();
    window.dispatchEvent(new Event("frameseek-catalog-ready"));
  })
  .catch(() => {
    notice("演示数据暂时无法加载，请刷新页面重试。");
    announce("演示数据加载失败。");
  });

window.FrameSeekSessionUI = {
  updateKnowledge,
  seek: (seconds, trigger) => seek(seconds, trigger),
  snapshot: () => ({ ...window.FrameSeekWorkbench.getSnapshot(), canvas: investigation?.snapshot(),
    ui: { sourceOpen: !document.querySelector(".agent-shell").classList.contains("source-collapsed"),
      inspectorOpen: inspectorIsOpen(),
      sourceWidth: panelWidths.source, inspectorWidth: panelWidths.inspector,
      playerTime: player.currentTime, draft: $("question").value } }),
  renameSource(name) {
    if(!activeVideo) return;
    activeVideo={...activeVideo,name};
    $("video-heading").textContent=name;$("attached-source-name").textContent=name;
    window.FrameSeekWorkbench.describe(activeVideo);
  },
  showSource: () => { setSourceOpen(true); $("source-dock").scrollTop = 0; },
  restoreDraft(value) { $("question").value = value; updateSend(); },
  currentContext: () => investigation?.context()?.query || null,
  currentContextRecordId: () => investigation?.context()?.turn.id || null,
  ready: () => !!demo,
  busy(value) {
    realBusy = value;
    $("question-form").setAttribute("aria-busy", String(value));
    $("send-question").setAttribute("aria-label", value ? "正在处理问题" : "发送消息");
    updateSend();
  },
  clear: clearChat,
  notify: notice,
  preflightFeedback,
  answer(query, submittedDraft) {
    renderAnswer(query);
    if (submittedDraft !== undefined && $("question").value.trim() === submittedDraft)
      $("question").value = "";
    updateSend();
  },
  applySource(video) {
    $("evidence-question").textContent = "素材预览";
    notice("");
    pendingTurnDeletion = null;
    if ($("delete-turn-dialog").open) $("delete-turn-dialog").close();
    locatingSeek = null;
    $("resume-query").hidden = true;
    activeVideo = video?.id === "demo_parcel" ? null : video;
    player.pause(); mediaReady = false; mediaFailed = false; pendingSeek = null;
    if (activeVideo) {
      setLocalMode(true);
      player.src = `/media/${video.id}/video`;
      const preview = video.metadata?.preview?.[0];
      if (preview) player.poster = preview.uri;
      else player.removeAttribute("poster");
      localMode = video.knowledge_status?.required_for_chat
        ? video.knowledge_status.state !== "ready"
        : video.job?.state !== "ready";
      window.FrameSeekWorkbench.source(localMode);
      window.FrameSeekWorkbench.describe(video);
      $("question").disabled = localMode;
      $("question").placeholder = localMode ? "素材尚未完成分析。" : "继续问这个视频…";
      $("video-heading").textContent = video.name;
      $("attached-source-name").textContent = video.name;
      $("mode-label").textContent = "真实素材";
      updateKnowledge(video);
      $("video-state").title = "私有素材 · 保存在本机服务";
      $("empty-eyebrow").textContent = "真实素材";
      $("canvas-hint").textContent = localMode ? "素材已保存 · 分析完成后即可提问" : "拖动空白平移 · Ctrl / ⌘ + 滚轮缩放";
      $("composer-mode-text").textContent = !localMode ? "证据问答" : "视频可播放 · 等待索引";
      $("source-thumbnail").hidden = !video.metadata?.preview?.length;
      if (video.metadata?.preview?.[0]) $("source-thumbnail").src = video.metadata.preview[0].uri;
      $("demo-notice-text").textContent = "素材保存在本机服务中。模型分析仅在已配置服务和已批准预算内执行。";
      $("local-note").textContent = "素材已上传至本机。每段素材拥有独立调查和证据。";
      $("restore-demo").hidden = false;
      $("suggestions").hidden = true;
      $("empty-state").querySelector("h1").textContent = localMode ? "素材已保存，等待分析。" : "从这段视频开始调查。";
      $("empty-state").querySelector(".task-intro > p").textContent = localMode ? "在素材库查看进度；配置完成后即可开始分析。" : "提问、回看证据，再从任意问题继续追问。";
      notice("");
    } else {
      $("knowledge-control").hidden = true;
      setLocalMode(false); player.src = "/demo/video"; player.poster = "/media/demo_parcel/frames/0.jpg";
      $("video-heading").textContent = "包裹移动 · 合成演示";
      $("attached-source-name").textContent = "包裹移动演示.mp4";
      $("source-thumbnail").src = "/media/demo_parcel/frames/0.jpg";
      $("source-thumbnail").hidden = false;
      window.FrameSeekWorkbench.describe({id:"demo_parcel",name:"包裹移动 · 合成演示",metadata:{duration:18,audio:false}});
    }
    player.load(); updateSend();
  },
  restore(saved, video) {
    this.applySource(video);
    taskSequence = Math.max(0, ...(saved.records || []).map(r => Number(r.id.split("-")[1]) || 0));
    for (const record of saved.records || []) if (!record.hidden) renderAnswer({ ...record.query, savedTurn: record.id });
    window.FrameSeekWorkbench.hydrate({ ...saved, local: localMode, sourceInfo: video });
    for (const pin of saved.pins || []) placeFinding(pin);
    if (saved.selectedId) investigation?.choose(saved.selectedId);
    investigation?.restore(saved.canvas);
    if (saved.ui) {
      setSourceOpen(saved.ui.sourceOpen !== false);
      restoreInspector(saved.ui.inspectorOpen !== false);
      $("question").value = saved.ui.draft || ""; updateSend();
      if (saved.ui.playerTime > 0) { pendingSeek = saved.ui.playerTime; pendingSeekRestore = true; }
    }
    restorePanelWidths(saved.ui);
  },
};

setupPanelResize();
