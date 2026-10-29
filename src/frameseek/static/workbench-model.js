/* Evidence collection state shared by React panels and the existing spatial engine. */
(function (root) {
  "use strict";
  const initial = () => ({
    epoch: 0,
    title: "未命名调查",
    groupTitle: "关键发现",
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
          title: `证据 ${clock(frame.t)}`,
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
          title: `结论 · ${query.question}`,
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
          `# ${state.title.trim() || "未命名调查"}`,
          "",
          state.sourceInfo?.id && state.sourceInfo.id !== "demo_parcel"
            ? `${state.sourceInfo.name} · 真实素材 · ${clock(state.sourceInfo.metadata?.duration || 0)}`
            : state.local
            ? "本地文件 · 未上传 · 未建立索引 · 未调用模型"
            : "合成演示 · 18 秒 · 预设结果 · 未调用模型",
          "",
          `## ${state.groupTitle.trim() || "关键发现"}`,
          "",
        ];
        for (const pin of state.pins) {
          if (pin.kind === "evidence")
            lines.push(
              `- ${clock(pin.frame.t)} — ${pin.frame.caption}`,
              `  来源问题：${pin.query.question}`,
            );
          else {
            lines.push(
              `- ${pin.query.response.answer}`,
              `  来源问题：${pin.query.question}`,
            );
            for (const text of pin.query.response.unresolved)
              lines.push(`  未确认：${text}`);
          }
        }
        if (!state.pins.length) lines.push("尚未保存发现。");
        lines.push(
          "",
          "## 调查笔记",
          "",
          state.note || "尚未添加笔记。",
          "",
          `本次调查：${state.records.length} 轮${state.sourceInfo?.id && state.sourceInfo.id !== "demo_parcel" ? "" : "预设"}问答。`,
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
    const real = videoId !== "demo_parcel";
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
      const window = `请查看视频${start}至${end}，`;
      const cached = [...new Set((record.query.response.followups || []).filter(text => typeof text === "string" && text.trim() && text.length <= 900))].slice(0, 3);
      if (cached.length) return {label:real ? "已有回答建议" : "演示追问", basis,
        items:cached.map(text => real && record !== selected ? `${window}${text}` : text)};
      if (real) {
        const text = [record.query.response.answer, ...frames.map(frame => frame.caption)].join(" ");
        const items = [], titles = [];
        const add = (title, question) => {titles.push(title); items.push(`${window}${question}`);};
        if (/记分牌|比分|scoreboard|\bscore\b/i.test(text))
          add("核对记分牌", "核对记分牌上可见的球队缩写和比分，引用清晰画面；看不清就说明。");
        if (/比赛时间|计时|第[一二三四]节|game clock|quarter|remaining/i.test(text))
          add("读取比赛计时", "读取比赛计时和节次，只报告清楚可见的数字，并引用画面证据。");
        if (/球衣|球场|篮球|basketball|jersey|court/i.test(text))
          add("描述球衣与站位", "描述球衣颜色和球员站位，不推测身份，并引用画面证据。");
        if (/纸箱|包裹|箱子|carton|parcel|cardboard|\bpackage\b/i.test(text)) {
          add("箱子有没有移动？", "核对箱子的位置是否发生变化；若有变化给出时间与画面证据，不清楚就说明。");
          add("箱子周围有什么？", "描述箱子与附近物体的可见位置关系，并引用画面证据。");
        }
        add("回看前后变化", "描述这张已保存画面前后有哪些可见变化，并引用对应时间的画面；不要补全未看见的动作。");
        add("哪些细节看不清？", "指出这张画面中不能可靠确认的细节，说明证据局限。");
        add("核对当前场景", "只描述该时段可见的场景与对象，并给出画面证据。");
        return {label:"基于已有证据", basis, titles:titles.slice(0,3), items:items.slice(0,3)};
      }
    }
    return real ? {label:"示例问题 · 尚无可用画面",items:[
      "概括这段视频里可见的场景和主要对象，并引用画面证据。",
      "找出一个画面清晰的片段，描述可见动作并给出视频时间。",
      "这段视频有哪些内容无法从画面中确认？请说明证据局限。",
    ]} : {label:"演示示例",items:["包裹什么时候出现在中央？","包裹什么时候移到右侧？","视频中有没有叉车？"]};
  }
  function queryFailureReason(query) {
    if (query?.mode !== "real" || query.response?.status !== "error") return "";
    const codes = (query.response.errors || []).map(error => error.code);
    if (codes.includes("budget_blocked")) return "预算准入未通过：该处理路径需要预留的请求次数或金额可能超过剩余额度，也可能是审批已暂停；不能据此判断模型没有找到画面。没有完成本轮视觉复核。";
    if (codes.includes("real_blocked")) return "这次请求在模型调用前被拦截：当时的服务未启用真实模型，或模型配置未就绪。不是模型看过视频后没有找到答案；可在「更多 → 模型与预算」检查当前配置。";
    if (codes.includes("audio_channel_unavailable")) return "尚无可靠音轨转写，无法回答解说内容；这不是问题写得不完整。";
    if (codes.includes("query_cancelled")) return "这次查找已取消，没有完成分析；不代表视频中没有相关内容。";
    if (codes.includes("invalid_tool_request")) return "分析工具请求未通过校验，未完成本轮查找；不是你的提问格式错误。";
    if (codes.includes("invalid_visual_matches")) return "模型返回的匹配条件、主体或画面引用未通过验证，未接受为证据。不能据此判断目标不存在；不会自动重试。";
    if (codes.includes("visual_index_invalid")) return "已有画面索引缺失、版本变化或引用无效，本次无法完成检索。原视频与聊天保留；不会自动重建索引或调用模型。";
    return "本轮分析未完成，系统没有保留足够信息来确认具体原因；不能据此判断视频中没有相关内容。";
  }
  function answerReviewNotes(answer) {
    const notes = [];
    for (const match of String(answer || "").matchAll(/从\s*(\d+(?:\.\d+)?)\s*(递减|下降|减少|递增|上升|增加)(?:至|到)\s*(\d+(?:\.\d+)?)/g)) {
      const before = Number(match[1]), after = Number(match[3]);
      if (!Number.isFinite(before) || !Number.isFinite(after)) continue;
      const decreases = ["递减", "下降", "减少"].includes(match[2]);
      if ((decreases && after > before) || (!decreases && after < before))
        notes.push(`文本一致性待核对：${match[1]} → ${match[3]} 与“${match[2]}”不一致。可能涉及计时重置或描述错误，请回看证据；原回答保留。`);
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
      heading: presentation.englishDemo ? `Replying to answer ${record.id.replace(/^task-/, "")}` : `引用问题 ${record.id.replace(/^task-/, "")} 的回答`,
      question: record.query.question,
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
        : null;
    const englishDemo = Boolean(englishPreset) && !query.synthetic_fixture
      && query.mode !== "real" && response.status === "ok" && video === "demo_parcel"
      && (response.events || []).length === 1 && response.events[0].video_id === "demo_parcel";
    return {
      text: englishDemo ? englishPreset : response.answer,
      label: englishDemo ? "Synthetic demo · preset answer" : query.synthetic_fixture ? "合成测试索引" : query.mode !== "real" ? "合成演示 · 预设回答" : query.engine === "index" ? "已有索引" : "模型回答",
      englishDemo,
      prefix: model && aggregation && numeric ? "模型给出的估计：" : "",
      caution: model && aggregation && ["ok", "partial"].includes(response.status)
        ? "这是模型回答，尚未核实整段视频的总数或排名。"
        : (response.unresolved || [])[0] || "",
      frames: [...frames.values()].sort((a,b)=>a.t-b.t),
      checkedFrames: [...checkedFrames.values()].sort((a,b)=>a.t-b.t),
      evidenceNote: englishDemo ? "Select a frame to replay its video time." : aggregation ? "引用画面的张数不代表事件发生次数。" : "点击画面，回看原视频对应时间。",
    };
  }
  const api = { replyQuote, conversationAnswer, shouldOfferTextCandidates, createStore, clock, visiblePins, toggleTurn, panelWidth, composerSuggestions, answerReviewNotes, queryFailureReason };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FrameSeekWorkbenchModel = api;
})(typeof window !== "undefined" ? window : globalThis);
