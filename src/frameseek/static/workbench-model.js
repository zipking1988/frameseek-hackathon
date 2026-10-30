/* Evidence collection state shared by React panels and the existing spatial engine. */
(function (root) {
  "use strict";
  const legacy = root.FrameSeekLegacyEnglish || (typeof module !== "undefined" && module.exports ? require("./legacy-demo-english.js") : null);
  const displayText = (value, query) => legacy?.display(value, query) ?? value;
  const initial = () => ({
    epoch: 0,
    title: "Untitled investigation",
    groupTitle: "Key findings",
    note: "",
    records: [],
    selectedId: null,
    pins: [],
    local: false,
    inspectorTab: "context",
    sourceInfo: null,
  });
  const clock = (value) =>
    `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
  function createStore() {
    let state = initial();
    let sequence = 0;
    const listeners = new Set();
    const publish = (changes) => {
      state = { ...state, ...changes };
      listeners.forEach((fn) => fn());
    };
    const store = {
      getSnapshot: () => state,
      subscribe: (fn) => {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
      hydrate(saved) {
        const clean = { ...initial(), ...saved };
        clean.records = (saved.records || []).slice(0, 100);
        clean.pins = (saved.pins || []).slice(0, 8);
        sequence = Math.max(0, ...clean.pins.map(p => Number(p.id.split("-").pop()) || 0));
        publish(clean);
      },
      describe(sourceInfo) { publish({ sourceInfo }); },
      record(id, query) {
        if (state.records.length >= 100 && !state.records.some(r => r.id === id)) return false;
        publish({
          records: [
            ...state.records.filter((r) => r.id !== id),
            { id, query },
          ],
          selectedId: id,
        });
        return true;
      },
      select(id) {
        if (state.records.some((r) => r.id === id && !r.hidden) && state.selectedId !== id)
          publish({ selectedId: id });
      },
      showPanel(tab) {
        if (["context", "findings"].includes(tab) && state.inspectorTab !== tab)
          publish({ inspectorTab: tab });
      },
      rename(title) {
        publish({ title: title.slice(0, 80) });
      },
      group(title) {
        publish({ groupTitle: title.slice(0, 60) });
      },
      annotate(note) {
        publish({ note: note.slice(0, 1200) });
      },
      source(local) {
        publish({ local });
      },
      clear() {
        sequence = 0;
        publish({ ...initial(), local: state.local, epoch: state.epoch + 1 });
      },
      pinFrame(turnId, query, frame) {
        if (
          state.local ||
          (state.sourceInfo?.id && query?.response.events.some(e => e.video_id !== state.sourceInfo.id)) ||
          !query?.response.events.some((event) =>
            event.evidence.some((f) => f.uri === frame.uri && f.t === frame.t),
          )
        )
          return null;
        const existing = state.pins.find(
          (p) =>
            p.kind === "evidence" &&
            p.frame.uri === frame.uri &&
            p.frame.t === frame.t,
        );
        if (existing) return existing;
        if (state.pins.length >= 8) return null;
        const pin = {
          id: `finding-${++sequence}`,
          kind: "evidence",
          title: `Evidence ${clock(frame.t)}`,
          frame: { ...frame },
          turnId,
          query,
        };
        publish({ pins: [...state.pins, pin] });
        return pin;
      },
      pinConclusion(turnId, query) {
        if (state.local || (state.sourceInfo?.id && query?.response.events.some(e => e.video_id !== state.sourceInfo.id)) || !["ok", "partial"].includes(query?.response.status))
          return null;
        const existing = state.pins.find(
          (p) => p.kind === "conclusion" && p.query.id === query.id,
        );
        if (existing) return existing;
        if (state.pins.length >= 8) return null;
        const pin = {
          id: `finding-${++sequence}`,
          kind: "conclusion",
          title: `Conclusion · ${query.question}`,
          turnId,
          query,
        };
        publish({ pins: [...state.pins, pin] });
        return pin;
      },
      remove(id) {
        publish({ pins: state.pins.filter((p) => p.id !== id) });
      },
      markdown() {
        if (root.FrameSeekReport) return root.FrameSeekReport.markdown(root.FrameSeekReport.create(state));
        const lines = [
          `# ${state.title.trim() || "Untitled investigation"}`,
          "",
          state.sourceInfo?.id && state.sourceInfo.id !== "demo_parcel"
            ? `${state.sourceInfo.name} · Uploaded video · ${clock(state.sourceInfo.metadata?.duration || 0)}`
            : state.local
            ? "Local video · Not uploaded or indexed · No model calls"
            : "Synthetic demo · 18 seconds · Preset results · No model calls",
          "",
          `## ${state.groupTitle.trim() || "Key findings"}`,
          "",
        ];
        for (const pin of state.pins) {
          if (pin.kind === "evidence")
            lines.push(
              `- ${clock(pin.frame.t)} — ${pin.frame.caption}`,
              `  Source question: ${pin.query.question}`,
            );
          else {
            lines.push(
              `- ${pin.query.response.answer}`,
              `  Source question: ${pin.query.question}`,
            );
            for (const text of pin.query.response.unresolved)
              lines.push(`  Unconfirmed: ${text}`);
          }
        }
        if (!state.pins.length) lines.push("No saved findings.");
        lines.push(
          "",
          "## Investigation notes",
          "",
          state.note || "No notes yet.",
          "",
          `This investigation contains ${state.records.length} question turns.`,
        );
        return lines.join("\n");
      },
    };
    return store;
  }
  function visiblePins(pins, kind = "all", order = "saved") {
    const result = pins.filter((pin) => kind === "all" || pin.kind === kind);
    if (order === "time")
      result.sort(
        (a, b) => (a.frame?.t ?? Infinity) - (b.frame?.t ?? Infinity),
      );
    return result;
  }
  function toggleTurn(state, id, hidden) {
    const record = state.records.find(r => r.id === id);
    if (!record || !!record.hidden === hidden) return null;
    const node = state.canvas?.nodes?.find(n => n.id === id) || record.hiddenNode;
    const nodes = (state.canvas?.nodes || []).filter(n => n.id !== id);
    if (!hidden && node) nodes.push(node);
    return { ...state,
      selectedId: hidden ? null : id,
      records: state.records.map(r => r.id === id ? {...r, hidden, hiddenNode: node} : r),
      canvas: {...state.canvas, nodes, selected: hidden ? null : id},
    };
  }
  function panelWidth(value, side, viewport, other) {
    const minimum = side === "source" ? 280 : 260;
    const maximum = Math.max(minimum, Math.min(side === "source" ? 900 : 600, viewport - 56 - 420 - other));
    return Math.round(Math.max(minimum, Math.min(maximum, Number.isFinite(value) ? value : minimum)));
  }
  function composerSuggestions(state) {
    const videoId = state.sourceInfo?.id || "demo_parcel";
    const real = !["demo_parcel", "demo_basketball"].includes(videoId);
    const duration = state.sourceInfo?.metadata?.duration;
    const candidates = (state.records || []).filter(record => {
      const q = record.query, r = q?.response;
      return !record.hidden && (real ? q?.mode === "real" && q.video_id === videoId : q?.mode !== "real")
        && ["ok", "partial"].includes(r?.status);
    });
    const framesFor = record => (record.query.response.events || []).filter(event => event.video_id === videoId)
      .flatMap(event => event.evidence || []).filter(frame =>
        Number.isFinite(frame.t) && frame.t >= 0 && (!Number.isFinite(duration) || frame.t <= duration)
        && typeof frame.uri === "string" && frame.uri.startsWith(`/media/${videoId}/frames/`));
    const selected = candidates.find(record => record.id === state.selectedId);
    const ordered = [...(selected ? [selected] : []), ...candidates.slice().reverse().filter(record => record !== selected)];
    const record = ordered.find(item => framesFor(item).length);
    if (record) {
      const frames = framesFor(record), anchor = frames[0];
      const basis = {t:anchor.t, uri:anchor.uri, queryId:record.query.id, turnId:record.id, videoId};
      const start = clock(Math.max(0, Math.floor(anchor.t - 6)));
      const end = clock(Math.ceil(Math.min(Number.isFinite(duration) ? duration : anchor.t + 6, anchor.t + 6)));
      const window = `Review the video from ${start} to ${end}: `;
      const cached = [...new Set((record.query.response.followups || []).filter(text => typeof text === "string" && text.trim() && text.length <= 900).map(text => displayText(text, record.query)))].slice(0, 3);
      if (cached.length) return {label:real ? "Suggested follow-ups" : "Demo follow-ups", basis,
        items:cached.map(text => real && record !== selected ? `${window}${text}` : text)};
      if (real) {
        const text = [record.query.response.answer, ...frames.map(frame => frame.caption)].join(" ");
        const items = [], titles = [];
        const add = (title, question) => {titles.push(title); items.push(`${window}${question}`);};
        if (/记分牌|比分|scoreboard|\bscore\b/i.test(text))
          add("Check the scoreboard", "Check the visible team abbreviations and score, citing clear frames. Say if they are unreadable.");
        if (/比赛时间|计时|第[一二三四]节|game clock|quarter|remaining/i.test(text))
          add("Read the game clock", "Read the game clock and quarter. Report only clearly visible numbers and cite the frames.");
        if (/球衣|球场|篮球|basketball|jersey|court/i.test(text))
          add("Describe jerseys and positions", "Describe jersey colors and player positions without guessing identities, and cite the frames.");
        if (/纸箱|包裹|箱子|carton|parcel|cardboard|\bpackage\b/i.test(text)) {
          add("Did the box move?", "Check whether the box changes position. Cite the times and frames, and say if it is unclear.");
          add("What is near the box?", "Describe the visible positions of the box and nearby objects, citing frames.");
        }
        add("Review what changed", "Describe visible changes around this saved frame and cite the corresponding times. Do not infer unseen actions.");
        add("What is unclear?", "Identify details that cannot be reliably confirmed and explain the limits of the evidence.");
        add("Review the scene", "Describe only the visible scene and objects in this interval, citing frame evidence.");
        return {label:"Based on saved evidence", basis, titles:titles.slice(0,3), items:items.slice(0,3)};
      }
    }
    return real ? {label:"Example questions · no evidence yet",items:[
      "Summarize the visible scene and main objects, citing video frames.",
      "Find a clear moment, describe the visible action, and give its timestamp.",
      "What cannot be confirmed from this footage? Explain the limits of the evidence.",
    ]} : videoId === "demo_basketball" ? {label:"Demo questions",items:[
      "Where is the ball handler in the court scene?",
      "What is visible in the pass scene?",
      "Where is the defender in the shot scene?",
    ]} : {label:"Demo questions",items:["When is the parcel in the center?","When is the parcel on the right?","Is there a forklift?"]};
  }
  function queryFailureReason(query) {
    if (query?.mode !== "real" || query.response?.status !== "error") return "";
    const codes = (query.response.errors || []).map(error => error.code);
    if (codes.includes("budget_blocked")) return "Budget admission failed: this route may require more requests or money than remain, or approval may be paused. Visual review did not complete; this does not mean the model found no frames.";
    if (codes.includes("real_blocked")) return "This request was blocked before a model call because live analysis was disabled or not configured. The model did not review the video. Check More → Models & budget.";
    if (codes.includes("audio_channel_unavailable")) return "A reliable audio transcript is unavailable, so commentary cannot be answered. This is not a problem with your question.";
    if (codes.includes("query_cancelled")) return "This search was cancelled before analysis completed; it does not establish that relevant content is absent.";
    if (codes.includes("invalid_tool_request")) return "The analysis tool request failed validation. The search did not complete; this is not a formatting error in your question.";
    if (codes.includes("invalid_visual_matches")) return "The model returned invalid match conditions, subjects, or frame references, so they were not accepted as evidence. This does not establish absence; the request will not retry automatically.";
    if (codes.includes("visual_index_invalid")) return "The frame index is missing, changed, or contains invalid references, so this search could not complete. The video and chat are preserved; the index will not be rebuilt and no model will be called automatically.";
    return "Analysis did not complete and there is not enough information to identify why. This does not establish that relevant content is absent.";
  }
  function answerReviewNotes(answer) {
    const notes = [];
    for (const match of String(answer || "").matchAll(/从\s*(\d+(?:\.\d+)?)\s*(递减|下降|减少|递增|上升|增加)(?:至|到)\s*(\d+(?:\.\d+)?)/g)) {
      const before = Number(match[1]), after = Number(match[3]);
      if (!Number.isFinite(before) || !Number.isFinite(after)) continue;
      const decreases = ["递减", "下降", "减少"].includes(match[2]);
      if ((decreases && after > before) || (!decreases && after < before))
        notes.push(`Review the numerical trend ${match[1]} → ${match[3]}; it conflicts with the stated direction. The original answer is retained; review the evidence.`);
      if (notes.length >= 2) break;
    }
    for (const match of String(answer || "").matchAll(/\b(decreases?|decreased|increases?|increased)\s+from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)/gi)) {
      const before = Number(match[2]), after = Number(match[3]);
      if ((/^decreas/i.test(match[1]) && after > before) || (/^increas/i.test(match[1]) && after < before))
        notes.push(`Review the numerical trend ${match[2]} → ${match[3]}; it conflicts with the stated direction. The original answer is retained; review the evidence.`);
      if (notes.length >= 2) break;
    }
    return notes;
  }
  function shouldOfferTextCandidates(query, available, accepts) {
    return Boolean(query?.engine === "index" && ["ok", "partial", "no_result"].includes(query.response?.status)
      && query.index_search?.ocr_eligible !== false
      && available && typeof accepts === "function"
      && accepts(query.index_search?.resolved_question || query.question));
  }
  function replyQuote(records, query, recordId = null) {
    if (!query) return null;
    const record = records.find(r => !r.hidden && r.query.id === query.id
      && r.query.video_id === query.video_id && (!recordId || r.id === recordId));
    if (!record) return null;
    const presentation = conversationAnswer(record.query);
    return {
      heading: `Replying to answer ${record.id.replace(/^task-/, "")}`,
      question: displayText(record.query.question, record.query),
      answer: presentation.text,
      ...(presentation.englishDemo ? {englishDemo: true} : {}),
    };
  }
  function conversationAnswer(query) {
    const response = query.response;
    const frames = new Map();
    const video = query.video_id || (query.mode !== "real" ? "demo_parcel" : null);
    const checkedFrames = new Map();
    if (query.engine === "index" && response.status === "no_result"
      && query.index_search?.kind === "supplement_unconfirmed"
      && Array.isArray(query.index_search.checked_frames)) {
      for (const frame of query.index_search.checked_frames.slice(0,16)) {
        const prefix = `/media/${video}/frames/`;
        if (!frame || typeof frame !== "object"
          || typeof frame.frame_id !== "string" || !frame.frame_id.startsWith(`${video}_`)
          || !Number.isFinite(frame.t) || frame.t < 0 || typeof frame.uri !== "string"
          || !frame.uri.startsWith(prefix)
          || !/^[a-zA-Z0-9_-]+\.jpg$/.test(frame.uri.slice(prefix.length))) continue;
        checkedFrames.set(frame.frame_id,frame);
      }
    }
    for (const event of response.events || []) {
      if (event.video_id && event.video_id !== video) continue;
      for (const frame of event.evidence || []) {
        if (!Number.isFinite(frame.t) || frame.t < 0 || typeof frame.uri !== "string"
          || !frame.uri.startsWith(`/media/${video}/frames/`)
          || frame.uri.includes("..") || frame.uri.includes("?")) continue;
        frames.set(frame.frame_id || frame.uri, frame);
      }
    }
    const aggregation = /多少(?:个|次|人|球)|几次|几个|数量|总数|最多|排名|how many|count|most|rank/i.test(query.question);
    const numeric = /^\s*\d+(?:\.\d+)?\s*(?:个|次|人|球)?[。.!！]?\s*$/.test(response.answer);
    const model = query.mode === "real" && query.engine !== "index";
    // These are translations of explicitly labelled presets, not a
    // translation or inference layer for arbitrary user video answers.
    const englishPreset = query.id === "center"
      && /^\s*when is the parcel in the center\??\s*$/i.test(query.question)
      && response.answer === "包裹在 00:05–00:11 出现在工作台中央。"
      ? "The parcel appears in the center from 00:05 to 00:11."
      : query.id === "right"
        && /^\s*(?:what happens next|when is the parcel on the right)\??\s*$/i.test(query.question)
        && response.answer === "从 00:11 开始，包裹出现在工作台右侧。"
        ? "From 00:11, the parcel appears on the right side of the workbench."
        : query.id === "forklift"
          && /^\s*is there a forklift\??\s*$/i.test(query.question)
          && response.answer === "这段 18 秒演示画面中没有叉车。"
          ? "There is no forklift in this 18-second synthetic demo."
          : null;
    const fixtureEvents = response.events || [];
    const englishDemo = Boolean(englishPreset) && !query.synthetic_fixture
      && query.mode !== "real" && video === "demo_parcel"
      && (response.status === "ok" && fixtureEvents.length === 1 && fixtureEvents[0].video_id === "demo_parcel"
        || response.status === "no_result" && fixtureEvents.length === 0 && query.id === "forklift");
    return {
      text: englishDemo ? englishPreset : displayText(response.answer, query),
      label: englishDemo ? "Synthetic demo · preset answer" : query.synthetic_fixture ? "Synthetic test index" : query.mode !== "real" ? "Synthetic demo · preset answer" : query.engine === "index" ? "Saved index" : "Model answer",
      englishDemo,
      prefix: model && aggregation && numeric ? "Model estimate: " : "",
      caution: model && aggregation && ["ok", "partial"].includes(response.status)
        ? "This model answer has not verified totals or rankings across the entire video."
        : displayText((response.unresolved || [])[0] || "", query),
      frames: [...frames.values()].map(frame => ({...frame, caption: displayText(frame.caption, query)})).sort((a,b)=>a.t-b.t),
      checkedFrames: [...checkedFrames.values()].sort((a,b)=>a.t-b.t),
      evidenceNote: englishDemo ? "Select a frame to replay its video time." : aggregation ? "The number of cited frames is not the number of events." : "Select a frame to replay the source video.",
    };
  }
  const api = { displayText, replyQuote, conversationAnswer, shouldOfferTextCandidates, createStore, clock, visiblePins, toggleTurn, panelWidth, composerSuggestions, answerReviewNotes, queryFailureReason };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FrameSeekWorkbenchModel = api;
})(typeof window !== "undefined" ? window : globalThis);
