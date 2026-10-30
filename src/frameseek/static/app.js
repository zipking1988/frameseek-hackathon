"use strict";
const $ = (id) => document.getElementById(id);
const player = $("player");
let demo = null;
const demoCatalogs = new Map();
async function prepareDemo(videoId) {
  if (demoCatalogs.has(videoId)) return demoCatalogs.get(videoId);
  const response = await fetch(`/demo/catalog?video_id=${encodeURIComponent(videoId)}`);
  if (!response.ok) throw new Error("Demo data could not load.");
  const catalog = await response.json();
  if (catalog.video?.id !== videoId) throw new Error("Demo source does not match.");
  demoCatalogs.set(videoId, catalog);
  return catalog;
}
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
    handle.setAttribute("aria-label", side === "source" ? "Adjust video panel width" : "Adjust inspector width");
    handle.title = "Drag to adjust width · Use arrow keys to fine-tune · Double-click to restore to default";
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
      handle.setAttribute("aria-valuetext", `${next} pixels`);
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
  $("rail-source").setAttribute("aria-label", open ? "Collapse video panel" : "Open video panel");
  $("rail-source").title = open ? "Collapse video panel" : "Open video panel";
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
    ? "Back to conversation "
    : "View video ";
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
  $("run-state").textContent = localMode ? "Not indexed" : "Waiting for tasks";
  updateMediaContext();
  $("result-step").textContent = localMode
    ? "Not yet connected to video search"
    : "Match from fixed examples";
  $("result-step-state").textContent = localMode ? "Not available" : "To be matched";
  $("evidence-step").textContent = localMode
    ? "There is no search evidence for this file"
    : "Time and screen connection";
  $("evidence-step-state").textContent = localMode ? "Not available" : "To be selected";
  document.querySelectorAll(".run-steps li").forEach((item, index) => {
    if (index > 0) item.classList.remove("complete");
  });
}

function knowledgeLabel(video) {
  const status = video?.knowledge_status;
  if (status?.state === "ready" && status.version) return "Index ready";
  if (status?.state === "indexing") return `Building index ${status.completed_segments}/${status.total_segments}`;
  if (status?.state === "waiting_probe") return "Waiting for video preflight";
  if (status?.state === "waiting_processor") return "Waiting for index configuration";
  if (status?.state === "waiting_budget") return "Waiting for budget or approval";
  if (status?.state === "failed" || status?.state === "unknown") return "Index incomplete";
  return "Index not yet built";
}

function updateKnowledge(video) {
  const status = video?.knowledge_status;
  const control = $("knowledge-control");
  $("video-state").textContent = knowledgeLabel(video);
  control.hidden = !status?.required_for_chat || status.state === "ready";
  control.disabled = status?.state === "waiting_probe" || status?.state === "unknown";
  control.textContent = ["queued", "indexing"].includes(status?.state) ? "Stop indexing" : "Build index";
}

function updateMediaContext() {
  if (activeVideo) {
    const ready = activeVideo.job?.state === "ready" || Number(activeVideo.metadata?.duration) > 0;
    $("attached-source-state").textContent = mediaFailed ? "Playback failed" : ready ? "Video ready" : "Uploaded · awaiting preflight";
    $("source-step").textContent = activeVideo.name;
    $("source-step-state").textContent = ready ? "Ready" : "Not ready";
    return;
  }
  $("source-step").textContent = mediaFailed
    ? "Video failed to load"
    : !mediaReady
      ? "Loading video"
      : localMode
        ? "Play in browser only"
        : "Synthetic video loaded";
  $("source-step-state").textContent = mediaFailed
    ? "Need to retry"
    : !mediaReady
      ? "Loading"
      : localMode
        ? "Not indexed"
        : "Ready";
  $("attached-source-state").textContent = mediaFailed
    ? "Loading failed"
    : !mediaReady
      ? "Loading"
      : localMode
        ? "Local play only · Not indexed"
        : "Loaded · 18 seconds";
  document
    .querySelector(".run-steps li:first-child")
    .classList.toggle("complete", mediaReady && !mediaFailed && !localMode);
}
const stateLabels = {
  ok: "Evidence available",
  partial: "Partially confirmed",
  no_result: "No matching frames",
  error: "Search incomplete",
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
  $("run-state").textContent = query?.engine==='index' ? "Index search completed" : stateLabels[data.status];
  $("result-step").textContent = query?.engine==='index' ? "Reading existing index · No model called" : activeVideo ? "Real model retrieval and review" : "Fixed demo sample matched";
  $("result-step-state").textContent = "Matched";
  document
    .querySelector(".run-steps li:nth-child(2)")
    .classList.add("complete");
  $("evidence-step").textContent = data.events.length
    ? `${data.events.length} events can be reviewed`
    : query?.engine==='index' ? "Description candidates can be reviewed on the index timeline" : "This sample has no targetable events";
  $("evidence-step-state").textContent = data.events.length
    ? "To be selected"
    : "no evidence";
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
    announce("The video cannot be located, please reload the video first.");
    $("seek-status").textContent = "The video cannot be located, please reload the video first.";
    return;
  }
  if (!mediaReady) {
    pendingSeek = seconds;
    pendingSeekTrigger = trigger;
    announce("The video is still loading and will be positioned to the corresponding screen after loading.");
    return;
  }
  const wasPaused = player.paused;
  locatingSeek = seconds;
  $("seek-status").textContent = `Locating ${clock(seconds)} · Waiting for the screen to load…`;
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
  $("evidence-step").textContent = `Locating ${clock(seconds)}`;
  $("evidence-step-state").textContent = "Loading";
  document.querySelector(".run-steps li:last-child").classList.remove("complete");
  completeSeek();
}
function completeSeek() {
  if (locatingSeek === null || player.seeking || player.readyState < 2 || Math.abs(player.currentTime - locatingSeek) > 0.25) return;
  const seconds = locatingSeek;
  locatingSeek = null;
  $("seek-status").textContent = `Located ${clock(seconds)} · ${player.paused ? "Paused" : "Playing"}`;
  $("evidence-step").textContent = `Targeted ${clock(seconds)}`;
  $("evidence-step-state").textContent = "Targeted";
  document.querySelector(".run-steps li:last-child").classList.add("complete");
  announce($("seek-status").textContent);
}
function updateTime() {
  if (investigation && mediaReady && locatingSeek === null)
    $("seek-status").textContent =
      `Current ${clock(player.currentTime)} · ${player.paused ? "Paused" : "Playing"}`;
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
    announce("This investigation is full. Your existing chats have been preserved; create another investigation to continue.");
    return;
  }
  if (++turns > 12 && !window.FrameSeekWorkbench) {
    const oldestId = $("messages").firstElementChild?.id;
    if (oldestId) document.querySelector(`[data-turn="${oldestId}"]`)?.remove();
    $("messages").firstElementChild?.remove();
  }
  const data = query.response;
  const display = value => window.FrameSeekWorkbenchModel?.displayText(value, query) ?? value;
  $("suggestions").hidden = true;
  const more = node("details", "response-details");
  more.append(node("summary", "", "More evidence and citations"));
  const turn = node("article", "turn question-node");
  turn.id = query.savedTurn || `task-${++taskSequence}`;
  taskSequence = Math.max(taskSequence, Number(turn.id.split("-")[1]) || 0);
  turn.dataset.kind = "question";
  turn.append(node("p", "question-bubble", display(query.question)));
  const label = node("div", "answer-label", "FrameSeek");
  label.append(
    node(
      "span",
      "fixture-tag",
      query.engine === "index" ? "Index Q&A · No model calls" : query.mode === "real" ? (query.usedContext ? "Evidence follow-up · Prior context" : "Model analysis") : query.usedContext ? "Preset follow-up · Prior context" : "Preset demo · No model calls",
    ),
  );
  turn.append(label, node("p", "answer-text", display(data.answer)));
  turn.append(
    node("span", `answer-status ${data.status}`, query.engine === "index" ? (data.events.length ? "Matching observations" : "Index result · Coverage incomplete") : stateLabels[data.status]),
  );
  if(query.engine==='index') turn.append(button('View index timeline · No model calls','outline-button',()=>window.FrameSeekPersistence?.searchIndex({question:query.index_search?.resolved_question || query.question,answerRequestId:query.id})));
  const eventCluster =
    data.events.length > 1 ? node("div", "event-cluster") : turn;
  if (eventCluster !== turn) turn.append(eventCluster);
  const previewEvents = ["local", "cloud", "index"].includes(query.engine)
    ? data.events.slice(0, 1) : data.events;
  previewEvents.forEach((event) => {
    const row = node("section", "event");
    row.append(node("h3", "event-heading", display(event.action)));
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
      node("span", "support-label", query.mode === "real" ? "Frame evidence · Verify its limits" : "Preset example · Supported by frames"),
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
      frameButton.setAttribute("aria-label", `Replay evidence frame at ${clock(frame.t)}`);
      const image = document.createElement("img");
      image.src = frame.uri;
      image.alt = display(frame.caption);
      image.loading = "lazy";
      const caption = node("span", "evidence-caption", clock(frame.t));
      caption.append(node("span", "", "View frame ↗"));
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
      button("Copy time reference", "text-button", async (eventClick) => {
        const trigger = eventClick.currentTarget;
        try {
          await navigator.clipboard.writeText(
            `${query.mode === "real" ? activeVideo?.name || query.video_id : "Synthetic demo: "+demo.video.title} ${clock(event.start)}–${clock(event.end)} — ${event.action}`,
          );
          trigger.textContent = "Copied";
          announce("The time reference of the current footage has been copied.");
        } catch {
          notice("Copying is unavailable in this browser. Select the time in the structured result.");
        }
      }),
    );
    eventCluster.append(row);
  });
  if (previewEvents.length < data.events.length) {
    const count = data.events.reduce((total, event) => total + event.evidence.length, 0);
    turn.append(button(`View all ${count} references`, "outline-button", () => {
      selectTask(data, historyButton, query);
      investigation?.choose(turn.id);
      openInspector("context");
    }));
  }
  if (data.unresolved.length) {
    const unresolved = node("div", "unresolved");
    unresolved.append(node("strong", "", "Unconfirmed"));
    data.unresolved.forEach((text) => unresolved.append(node("p", "", display(text))));
    (investigation ? more : turn).append(unresolved);
  }
  if (data.errors.length) {
    turn.append(
      node("p", "trace-note", data.errors[0].message),
      button(query.mode === "real" ? "Edit question" : "Choose another example", "text-button", () => {
        if (query.mode === "real") window.FrameSeekUI.continueFrom(turn.id);
        else {
          $("question").focus();
          notice("Choose a suggested demo question.");
        }
      }),
    );
  } else {
    turn.append(
      node(
        "p",
        "trace-note",
        query.engine==='index' ? "Existing records searched · No new visual review" : query.mode === "real" ? `Search and review · ${data.trace.visible.segments_reviewed} segments` : "Synthetic demo · 00:00–00:18 · No audio or model evaluation",
      ),
    );
  }
  const details = node("details", "response-details");
  details.append(
    node("summary", "", "View structured result"),
    node("pre", "", JSON.stringify(data, null, 2)),
  );
  more.append(details);
  const actions = node("div", "response-actions");
  data.followups.forEach((question) =>
    actions.append(
      button(display(question), "text-button", () => {
        fillSuggestedQuestion(display(question), turn.id);
      }),
    ),
  );
  more.append(actions);
  turn.append(more);
  $("empty-state").hidden = true;
  $("messages").append(turn);
  const historyButton = button(display(query.question), "history-task", () => {
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
  historyButton.title = display(query.question);
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
  const spoken = window.FrameSeekWorkbenchModel?.conversationAnswer(query).text || data.answer;
  announce(`${query.mode === "real" ? "Answer" : "Demo answer"}: ${query.engine==='index' ? "Index search complete" : stateLabels[data.status]}. ${spoken}`);
}
function fillSuggestedQuestion(question, turnId = null) {
  const input = $("question");
  if (input.value.trim()) {
    notice("Your draft was kept. Edit or clear it before selecting a suggested question.");
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
    const display = value => demo?.video?.id === "demo_parcel" ? window.FrameSeekLegacyEnglish?.text(value) || value : value;
    const supported = (scoped?.query.suggested_replies || []).map(display);
    notice(supported.length
      ? `Try a supported follow-up: ${supported.join(" · ")}. Your draft was kept.`
      : `Try a demo question: ${display(demo?.queries?.[0]?.question) || "Choose a suggested question."} Your draft was kept.`);
    announce("This question is not a demo sample and the model is not called.");
    return;
  }
  notice("");
  lastQueryId = resolved.query.id;
  renderAnswer({
    ...resolved.query,
    question,
    video_id: demo.video.id,
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
    announce("This demo cannot be loaded, please choose another investigation path.");
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
      question: demo.video.id === "demo_parcel" ? window.FrameSeekLegacyEnglish?.text(seed.question) || seed.question : seed.question,
      video_id: demo.video.id,
      parentTurn: seed.parentIndex === null ? null : turnIds[seed.parentIndex],
    });
    turnIds.push(`task-${taskSequence}`);
    lastQueryId = seed.id;
  }
  investigation?.arrange(scenario.layout || "threads");
  const scenarioTitle = demo.video.id === "demo_parcel" ? window.FrameSeekLegacyEnglish?.text(scenario.title) || scenario.title : scenario.title;
  $("demo-current").textContent = `${scenarioTitle} · ${seeds.length} preset turns`;
  $("demo-current").hidden = false;
  window.FrameSeekWorkbench?.rename(`${scenarioTitle} investigation`);
  if (window.FrameSeekWorkbench && window.innerWidth < 1400) {
    $("zoom-reset").click();
    investigation.choose(turnIds[0]);
    investigation.reveal(turnIds[0]);
  }
  $("question").focus();
  announce(
    `Loaded ${scenarioTitle}, with ${seeds.length} preset turns. Choose a card to continue.`,
  );
}
function renderScenarios() {
  if (!demo.scenarios?.length) return;
  const list = $("demo-scenarios");
  const display = value => demo.video.id === "demo_parcel" ? window.FrameSeekLegacyEnglish?.text(value) || value : value;
  list.replaceChildren();
  demo.scenarios.forEach((scenario, index) => {
    const entry = button("", "demo-scenario", () => startScenario(scenario.id));
    entry.setAttribute("aria-label", `Load the demo: ${display(scenario.title)}`);
    entry.append(
      node("span", "demo-scenario-number", String(index + 1).padStart(2, "0")),
      node("strong", "", display(scenario.title)),
      node("p", "", display(scenario.description)),
      node(
        "span",
        "demo-scenario-focus",
        `${display(scenario.focus)} · ${scenario.steps.length} turns`,
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
  $("mode-label").textContent = enabled ? "Play locally" : "Demo mode";
  $("demo-notice-text").textContent = enabled
    ? "Local playback mode. The file is only opened in this browser and is not uploaded, indexed, or sent to the model."
    : "This is a synthetic demo with preset answers and follow-ups. No file is uploaded and no model is called.";
  $("restore-demo").hidden = !enabled;
  $("suggestions").hidden = enabled;
  $("composer-mode-text").textContent = enabled
    ? "Local playback only · Not indexed"
    : "Synthetic demo · Preset answers · No model calls";
  $("demo-timeline").hidden = enabled;
  $("segments").hidden = enabled;
  $("frame-heading").hidden = enabled;
  $("empty-eyebrow").textContent = enabled ? "Local playback only" : "Demo video loaded";
  $("local-note").hidden = !enabled;
  $("video-watermark").hidden = enabled;
  $("question").disabled = enabled;
  $("question").placeholder = enabled
    ? "The current video is not indexed, so questions cannot be asked at the moment."
    : "Keep asking, or ask new questions…";
  document.querySelectorAll("[data-query]").forEach((element) => {
    element.disabled = enabled || !demo;
  });
  $("video-state").textContent = enabled ? "Local playback only" : "Demo source";
  document.querySelector(".source-item span").textContent = enabled
    ? "Local video · Not indexed"
    : "Synthetic demo";
  $("attached-source-state").textContent = enabled
    ? "Local play only · Not indexed"
    : "Loaded · 18 seconds";
  $("empty-state").querySelector(investigation ? "h1" : "h2").textContent =
    enabled
      ? "The footage is open and not indexed yet."
      : investigation
        ? "Start with a question and start an investigation."
        : "Which picture are you looking for?";
  $("empty-state").querySelector(".task-intro > p").textContent = enabled
    ? "You can play the file in the video workspace, or return to the demo experience preset session."
    : investigation
      ? "The found footage will remain here. Keep asking and connect the dots."
      : "Choose a question, review its evidence, and follow up.";
  $("video-state").title = enabled ? "Not uploaded, not analyzed" : "Publicly available composite material";
  $("seek-status").textContent = enabled
    ? "Not uploaded · Not analyzed · File can only be played in the browser"
    : "Click on the time or evidence frame to navigate to the corresponding screen";
  clearChat();
  if (enabled) {
    notice("Local video opened. Return to the demo to try evidence navigation.");
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
    announce("This screen cannot be saved. Please use the Save button on the referenced screen.");
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
      `Looking back at canvas evidence ${clock(pin.frame.t)}`,
    );
    const image = document.createElement("img");
    image.src = pin.frame.uri;
    image.alt = window.FrameSeekWorkbenchModel.displayText(pin.frame.caption, pin.query);
    frameButton.append(
      image,
      node("span", "evidence-caption", clock(pin.frame.t)),
    );
    card.append(frameButton, node("p", "artifact-caption", window.FrameSeekWorkbenchModel.displayText(pin.frame.caption, pin.query)));
  } else {
    card.append(node("p", "answer-text", window.FrameSeekWorkbenchModel.displayText(pin.query.response.answer, pin.query)));
    pin.query.response.unresolved.forEach((text) =>
      card.append(node("p", "artifact-unresolved", window.FrameSeekWorkbenchModel.displayText(text, pin.query))),
    );
  }
  card.append(node("p", "artifact-source", `Source: ${window.FrameSeekWorkbenchModel.displayText(pin.query.question, pin.query)}`));
  card.append(
    button("Remove from findings", "text-button", () => removeFinding(pin.id)),
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
    `saved${pin.kind === "evidence" ? "evidence frame" : "conclusion"}, can be moved on the canvas.`,
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
  announce("Removed from discovery collection and can be saved again.");
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
      notice(`${window.FrameSeekWorkbenchModel.queryFailureReason(query)} Your question was kept for editing. Nothing will be retried automatically; sending requires a budget check.`);
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
  $("delete-turn-question").textContent = window.FrameSeekWorkbenchModel.displayText(record.query.question, record.query);
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
  notice(hidden ? "Question removed from the canvas. Restore it from deleted questions; saved evidence remains." : "Question restored. Your draft and canvas position were kept.");
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
    notice("This video is available for local playback. Return to the demo to ask questions.");
  }
  showEvidence(false);
  if (!localMode) $("question").focus();
  announce("Presentation dialog has been reset.");
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
  $("video-heading").textContent = "Parcel movement · synthetic demo";
  $("attached-source-name").textContent = "Parcel moving demonstration.mp4";
  $("video-meta").textContent = "18 seconds · 1600 × 900 · No audio track";
  $("file-input").value = "";
  showEvidence(false);
  announce("The fixed demo has been returned and the original local file was not uploaded.");
});
function mediaLoaded() {
  mediaReady = true;
  mediaFailed = false;
  $("reload-video").hidden = true;
  updateMediaContext();
  if (localMode || activeVideo)
    $("video-meta").textContent =
      `${clock(player.duration)} · ${player.videoWidth} × ${player.videoHeight} · ${activeVideo?.job?.state === "ready" || Number(activeVideo?.metadata?.duration) > 0 ? "Video preflight complete" : "Video preflight incomplete"}`;
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
    "The video cannot be played, please reload or select a format supported by your browser.";
  $("reload-video").hidden = false;
  announce($("seek-status").textContent);
});
$("reload-video").addEventListener("click", () => {
  locatingSeek = null;
  mediaFailed = false;
  mediaReady = false;
  updateMediaContext();
  player.load();
  $("seek-status").textContent = "Reloading video…";
});
player.addEventListener("timeupdate", updateTime);
player.addEventListener("seeking", () => {
  if (locatingSeek !== null && Math.abs(player.currentTime - locatingSeek) > 0.25) {
    locatingSeek = player.currentTime;
    $("seek-status").textContent = `Locating ${clock(locatingSeek)} · Waiting for the screen to load…`;
    $("evidence-step").textContent = `Locating ${clock(locatingSeek)}`;
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
prepareDemo("demo_basketball")
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
    notice("Demo data could not load. Refresh the page to try again.");
    announce("Demo data failed to load.");
    window.dispatchEvent(new Event("frameseek-catalog-error"));
  });

window.FrameSeekSessionUI = {
  prepareDemo,
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
    $("send-question").setAttribute("aria-label", value ? "Processing question" : "Send message");
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
    $("evidence-question").textContent = "Video preview";
    notice("");
    pendingTurnDeletion = null;
    if ($("delete-turn-dialog").open) $("delete-turn-dialog").close();
    locatingSeek = null;
    $("resume-query").hidden = true;
    const isDemo = ["demo_parcel", "demo_basketball"].includes(video?.id);
    activeVideo = isDemo ? null : video;
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
      $("question").placeholder = localMode ? "Video analysis is not ready." : "Ask a question about this video…";
      $("video-heading").textContent = video.name;
      $("attached-source-name").textContent = video.name;
      $("mode-label").textContent = "Uploaded video";
      updateKnowledge(video);
      $("video-state").title = "Private source · stored on this server";
      $("empty-eyebrow").textContent = "Uploaded video";
      $("canvas-hint").textContent = localMode ? "Video saved · Questions available after analysis" : "Drag blank space to pan · Ctrl / ⌘ + scroll wheel to zoom";
      $("composer-mode-text").textContent = !localMode ? "Evidence Q&A" : "Video available · awaiting index";
      $("source-thumbnail").hidden = !video.metadata?.preview?.length;
      if (video.metadata?.preview?.[0]) $("source-thumbnail").src = video.metadata.preview[0].uri;
      $("demo-notice-text").textContent = "The video is stored on this server. Model analysis requires a configured service and an approved budget.";
      $("local-note").textContent = "Video uploaded to this server. Each source has its own investigations and evidence.";
      $("restore-demo").hidden = false;
      $("suggestions").hidden = true;
      $("empty-state").querySelector("h1").textContent = localMode ? "The video is saved and awaits analysis." : "Start your investigation with this video.";
      $("empty-state").querySelector(".task-intro > p").textContent = localMode ? "Check progress in Manage videos. Analysis requires a configured route." : "Ask questions, review the evidence, and continue from any question.";
      notice("");
    } else {
      demo = demoCatalogs.get(video.id);
      if (!demo) throw new Error("Demo catalog is unavailable.");
      renderScenarios();
      $("knowledge-control").hidden = true;
      setLocalMode(false); player.src = demo.video.uri; player.poster = `/media/${video.id}/frames/0.jpg`;
      $("video-heading").textContent = video.name;
      $("attached-source-name").textContent = video.name;
      $("video-meta").textContent = "18 seconds · 1600 × 900 · No audio track";
      $("source-thumbnail").src = `/media/${video.id}/frames/0.jpg`;
      $("source-thumbnail").hidden = false;
      $("demo-notice-text").textContent = video.id === "demo_basketball"
        ? "Generated stills and preset answers. No model calls. The stills do not establish continuous play, player identity, or whether the shot scores. Earlier Nemotron tests produced separate saved partial results."
        : "Synthetic parcel demo with preset answers. No model calls.";
      document.querySelectorAll(".source-filmstrip .segment").forEach((segment, index) => {
        const seconds = [0, 6, 13][index];
        segment.querySelector("img").src = `/media/${video.id}/frames/${seconds}.jpg`;
        segment.querySelector("img").alt = video.id === "demo_basketball" ? ["Synthetic court scene", "Synthetic pass scene", "Synthetic shot scene"][index] : ["Parcel on the left", "Parcel in the center", "Parcel on the right"][index];
        segment.setAttribute("aria-label", `Replay ${video.id === "demo_basketball" ? ["court", "pass", "shot"][index] : ["left", "center", "right"][index]} scene at ${clock(seconds)}`);
      });
      document.querySelectorAll("#demo-timeline .chapter").forEach((chapter, index) => {
        const title = video.id === "demo_basketball" ? ["Court", "Pass", "Shot"][index] : ["Left", "Center", "Right"][index];
        chapter.textContent = title; chapter.setAttribute("aria-label", `${title} scene at ${clock(Number(chapter.dataset.time))}`);
      });
      window.FrameSeekWorkbench.describe(video);
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
