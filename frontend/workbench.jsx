import React, {
  useSyncExternalStore,
  useRef,
  useState,
  useEffect,
  memo,
} from "react";
import { createRoot } from "react-dom/client";

const store = window.FrameSeekWorkbenchModel.createStore();
window.FrameSeekWorkbench = store;
const clock = window.FrameSeekWorkbenchModel.clock;
const useDesk = () => useSyncExternalStore(store.subscribe, store.getSnapshot);
const ui = () => window.FrameSeekUI;
const icon = (name) => (
  <img className="icon" src={`/static/icons/${name}.svg`} alt="" />
);
const labels = {
  ok: "画面可确认",
  partial: "仍有未确认项",
  no_result: "未找到画面",
  error: "未完成",
};

// Synchronize the persistent header controls without remounting their listeners.
function WorkspaceChrome() {
  const state = useDesk();
  useEffect(() => {
    const count = document.getElementById("rail-findings-count");
    count.textContent = state.pins.length;
    count.hidden = !state.pins.length;
  }, [state.pins.length]);
  useEffect(() => {
    const menu = document.getElementById("header-more");
    const trigger = menu.querySelector("summary");
    const dismiss = (event) => {
      if (!menu.contains(event.target)) menu.open = false;
    };
    const escape = (event) => {
      if (event.key !== "Escape" || !menu.open) return;
      event.preventDefault();
      menu.open = false;
      trigger.focus();
    };
    const activate = (event) => {
      if (event.target.closest("button, a")) menu.open = false;
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    menu.addEventListener("click", activate);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
      menu.removeEventListener("click", activate);
    };
  }, []);
  useEffect(() => {
    const menu = document.getElementById("investigation-menu");
    if (!menu?.querySelector) return;
    const closeOutside = event => {if (!menu.contains(event.target)) menu.open = false;};
    const escape = event => {
      if (event.key === "Escape" && menu.open) {event.preventDefault(); menu.open = false; menu.querySelector("summary").focus();}
    };
    const activate = event => {if (event.target.closest("button")) menu.open = false;};
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", escape);
    menu.addEventListener("click", activate);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", escape);
      menu.removeEventListener("click", activate);
    };
  }, []);
  return null;
}

function WorkspaceView() {
  const [mode, setMode] = useState(() => {
    try { return localStorage.getItem("frameseek-reading-view") === "canvas" ? "canvas" : "conversation"; }
    catch { return "conversation"; }
  });
  useEffect(() => {
    document.querySelector(".agent-shell").classList.toggle("conversation-mode", mode === "conversation");
    document.getElementById("chat-content").setAttribute("aria-label", mode === "conversation" ? "视频对话" : "调查画布");
    document.getElementById("chat-content").dataset.readingMode = String(mode === "conversation");
    try { localStorage.setItem("frameseek-reading-view", mode); } catch {}
    document.getElementById("reading-tools").open = mode === "canvas";
    document.getElementById("engine-settings").open = false;
    window.dispatchEvent(new Event("frameseek-reading-view"));
    window.dispatchEvent(new Event("resize"));
  }, [mode]);
  useEffect(() => {
    const form = document.getElementById("question-form");
    const enterConversation = event => {
      if (event.type === "focusin" ? event.target.id !== "question"
        : event.target.closest("button, select, summary, a")) return;
      setMode("conversation");
    };
    form.addEventListener("pointerdown", enterConversation);
    form.addEventListener("focusin", enterConversation);
    return () => {
      form.removeEventListener("pointerdown", enterConversation);
      form.removeEventListener("focusin", enterConversation);
    };
  }, []);
  return <div className="reading-tabs" role="group" aria-label="工作区视图">
    <button type="button" aria-pressed={mode === "conversation"} onClick={() => setMode("conversation")}>对话</button>
    <button type="button" aria-pressed={mode === "canvas"} onClick={() => setMode("canvas")}>画布</button>
  </div>;
}
function ConversationContext() {
  const state = useDesk();
  const [selection, setSelection] = useState(null);
  useEffect(() => {
    const label = document.getElementById("composer-label"), clear = document.getElementById("clear-context");
    const sync = () => setSelection(clear.hidden ? null : {
      query: window.FrameSeekSessionUI?.currentContext() || null,
      recordId: window.FrameSeekSessionUI?.currentContextRecordId() || null,
    });
    const observer = new MutationObserver(sync);
    observer.observe(label, {childList:true, subtree:true, characterData:true});
    observer.observe(clear, {attributes:true, attributeFilter:["hidden"]});
    sync(); return () => observer.disconnect();
  }, []);
  const context = window.FrameSeekWorkbenchModel.replyQuote(state.records, selection?.query, selection?.recordId);
  useEffect(() => {
    document.getElementById("conversation-context-chip").hidden = !context;
    const input = document.getElementById("question");
    if (!input.disabled) input.placeholder = context?.englishDemo ? "Ask a follow-up…" : context ? "针对这条回答继续提问…" : "向这个视频提问…";
  }, [context?.heading, context?.answer, state.sourceInfo?.id]);
  return context ? <div className="reading-context-chip" role="group" aria-label={context.heading}>
    <div className="reading-quote-copy">
      <strong>{context.heading}</strong>
      <p className="reading-quote-question" title={context.question}>{context.question}</p>
      <p className="reading-quote-answer" title={context.answer}>{context.englishDemo ? "Answer: " : "回答："}{context.answer}</p>
    </div>
    <button type="button" aria-label={context.englishDemo ? "Remove reference, keep draft" : "取消引用，保留草稿"} onClick={() => document.getElementById("clear-context").click()}>{context.englishDemo ? "Remove reference" : "取消引用"}</button>
  </div> : null;
}
const ConversationTurn = memo(function ConversationTurn({record, selected, ocrAvailable}) {
  const [expanded, setExpanded] = useState(false);
  const [checkedExpanded, setCheckedExpanded] = useState(false);
  const query = record.query, response = query.response;
  const presentation = window.FrameSeekWorkbenchModel.conversationAnswer(query);
  const frames = expanded ? presentation.frames : presentation.frames.slice(0,2);
  const replay = frame => { ui()?.seek(frame.t, query); };
  return <article id={`conversation-${record.id}`} className={`conversation-turn${selected ? " is-selected" : ""}`} aria-label={`问题与回答：${query.question}`}>
    <div className="reading-question">{icon("chats-circle")}<p>{query.question}</p></div>
    <div className="reading-answer">
      <span className="reading-mark">{icon("chats-circle")}</span>
      <span className="reading-speaker">FrameSeek <span>{presentation.label}</span></span>
      <p className="reading-answer-text">{presentation.prefix}{presentation.text}</p>
      {presentation.caution && <p className="reading-caution">{presentation.caution}</p>}
      {!!frames.length && <div className="reading-evidence-grid">{frames.map(frame => <button className="reading-evidence" type="button" key={frame.frame_id || frame.uri}
        aria-label={`${presentation.englishDemo ? "Replay evidence" : "回看证据"} ${clock(frame.t)}`} onClick={() => replay(frame)}>
        <img src={frame.uri} alt={presentation.englishDemo ? "Synthetic parcel demo evidence" : frame.caption || "原视频引用画面"} loading="lazy" decoding="async"/>
        <span>{icon("play")}<time>{clock(frame.t)}</time><strong>{presentation.englishDemo ? "Replay" : "回看"}</strong></span>
      </button>)}</div>}
      {!!presentation.frames.length && <p className="reading-evidence-note">{presentation.frames.length} {presentation.englishDemo ? (presentation.frames.length === 1 ? "cited frame" : "cited frames") : "张引用画面"} · {presentation.evidenceNote}</p>}
      {presentation.frames.length > 2 && <button type="button" className="text-button reading-show-all" onClick={() => setExpanded(!expanded)}>{expanded ? "收起画面" : `查看全部 ${presentation.frames.length} 张画面`}</button>}
      {!!presentation.checkedFrames.length && <>
        <p className="reading-evidence-note">本次补查的抽样画面 · 不是匹配证据，点击回看原视频。</p>
        <div className="reading-evidence-grid">{(checkedExpanded ? presentation.checkedFrames : presentation.checkedFrames.slice(0,2)).map(frame =>
          <button className="reading-evidence" type="button" key={frame.frame_id} aria-label={`回看已检查画面 ${clock(frame.t)} · 非匹配证据`} onClick={() => replay(frame)}>
            <img src={frame.uri} alt="已检查的抽样画面，不代表匹配" loading="lazy" decoding="async"/>
            <span>{icon("play")}<time>{clock(frame.t)}</time><strong>回看</strong></span>
          </button>)}</div>
        {presentation.checkedFrames.length > 2 && <button type="button" className="text-button reading-show-all" onClick={() => setCheckedExpanded(!checkedExpanded)}>{checkedExpanded ? "收起已检查画面" : `查看全部 ${presentation.checkedFrames.length} 张已检查画面`}</button>}
      </>}
      <div className="reading-response-actions">
        <button type="button" className="text-button" onClick={() => ui()?.continueFrom(record.id)}>{presentation.englishDemo ? "Ask a follow-up" : query.mode === "real" && !response.events.length ? "重新编辑问题" : "追问这条回答"}</button>
        {query.engine === "index" && <button type="button" className="text-button" onClick={() => window.FrameSeekPersistence?.searchIndex({question: query.index_search?.resolved_question || query.question, answerRequestId: query.id})}>查看索引与候选画面</button>}
        {window.FrameSeekWorkbenchModel.shouldOfferTextCandidates(query, ocrAvailable, window.FrameSeekPersistence?.canSearchOcr) && <button type="button" className="text-button" onClick={() => window.FrameSeekPersistence?.searchIndex({question: query.index_search?.resolved_question || query.question, answerRequestId: query.id, autoOcr: true})}>查找更多文字候选 · 不调用模型</button>}
        <details className="reading-basis"><summary>查看依据与操作</summary>
          <p>{response.events.length} 组画面 · {presentation.frames.length} 张引用</p>
          {(response.unresolved || []).map((text,index) => <p key={index}>{text}</p>)}
          {response.status === "error" && <p>{window.FrameSeekWorkbenchModel.queryFailureReason(query)}</p>}
          <button type="button" className="text-button" onClick={() => ui()?.saveConclusion(record.id,query)}>保存结论</button>
          <button type="button" className="text-button danger" onClick={() => ui()?.hideTurn(record.id)}>删除问题</button>
        </details>
      </div>
    </div>
  </article>;
});
function Conversation() {
  const state = useDesk();
  const records = state.records.filter(record => !record.hidden);
  const scrollSelected = () => {
    if (!document.querySelector(".agent-shell").classList.contains("conversation-mode")) return;
    document.getElementById(`conversation-${store.getSnapshot().selectedId}`)?.scrollIntoView({block:"end",behavior:"instant"});
  };
  useEffect(scrollSelected, [state.selectedId, records.length]);
  useEffect(() => {
    const resized = () => requestAnimationFrame(scrollSelected);
    window.addEventListener("resize", resized);
    window.addEventListener("frameseek-thread-focus",scrollSelected);
    window.addEventListener("frameseek-reading-view",scrollSelected);
    return () => {window.removeEventListener("resize", resized);window.removeEventListener("frameseek-thread-focus",scrollSelected);window.removeEventListener("frameseek-reading-view",scrollSelected);};
  }, []);
  return <div className="conversation-stream" role="region" aria-label="问题、回答与视频证据">
    {!records.length && <div className="reading-empty"><p className="reading-eyebrow">问视频，回看证据</p><h1>从一个问题开始。</h1><p>答案和对应画面会一起留在这里。</p>
      {state.sourceInfo?.id === "demo_parcel" && !state.local && <div className="reading-demo-examples">
        <p>试试合成演示 · 预设回答，不调用模型</p>
        <div className="suggestion-buttons">{window.FrameSeekWorkbenchModel.composerSuggestions(state).items.map(question =>
          <button key={question} type="button" onClick={() => ui()?.fillSuggestedQuestion(question)}>{question}</button>)}</div>
        <small>点击填入，确认后发送。</small>
      </div>}
    </div>}
    {records.map(record => <ConversationTurn key={record.id} record={record} selected={record.id===state.selectedId} ocrAvailable={!!state.sourceInfo?.knowledge_status?.ocr_candidates_available}/>)}
  </div>;
}

function SourcePicker() {
  const [sources, setSources] = useState({items: [], selected: "", busy: true});
  const [open, setOpen] = useState(false);
  const root = useRef(null), trigger = useRef(null), panel = useRef(null);
  const returnFocus = useRef(false);
  useEffect(() => {
    const select = document.getElementById("source-switcher");
    const read = () => setSources({
      selected: select.value, busy: select.disabled,
      items: Array.from(select.options, option => ({
        id: option.value, name: option.textContent,
        thumbnail: option.dataset.thumbnail, detail: option.dataset.detail,
      })),
    });
    read();
    select.addEventListener("frameseek-sources", read);
    return () => select.removeEventListener("frameseek-sources", read);
  }, []);
  useEffect(() => {
    if (!open) return;
    (panel.current.querySelector('[aria-current="true"]') || panel.current.querySelector("button"))?.focus();
    const outside = event => { if (!root.current.contains(event.target)) setOpen(false); };
    const escape = event => {
      if (event.key !== "Escape") return;
      event.preventDefault(); setOpen(false); trigger.current.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  useEffect(() => {
    if (sources.busy || !returnFocus.current) return;
    returnFocus.current = false;
    if (document.activeElement === document.body) trigger.current.focus();
  }, [sources.busy]);
  const current = sources.items.find(item => item.id === sources.selected);
  const thumb = item => item?.thumbnail
    ? <img className="source-picker-thumb" src={item.thumbnail} alt="" onError={event => {event.currentTarget.style.visibility = "hidden";}} />
    : <span className="source-picker-thumb source-picker-placeholder">{icon("play")}</span>;
  const choose = id => {
    returnFocus.current = id !== sources.selected;
    setOpen(false); trigger.current.focus();
    const select = document.getElementById("source-switcher");
    select.value = id;
    select.dispatchEvent(new Event("change", {bubbles: true}));
  };
  const navigate = event => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = Array.from(panel.current.querySelectorAll("button"));
    const index = buttons.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  };
  return <div className="source-picker" ref={root}>
    <button type="button" className="source-picker-trigger" ref={trigger}
      aria-label={`切换视频：${current?.name || "正在读取"}`} aria-haspopup="dialog"
      aria-expanded={open} aria-controls="source-picker-panel" disabled={sources.busy}
      onClick={() => setOpen(value => !value)} onKeyDown={event => {
        if (event.key === "ArrowDown") {event.preventDefault(); setOpen(true);}
      }}>
      {thumb(current)}
      <span className="source-picker-copy"><small>{sources.busy ? "正在切换…" : "当前视频"}</small>
        <strong>{current?.name || "正在读取视频…"}</strong></span>
      <span className="source-picker-chevron" aria-hidden="true">⌄</span>
    </button>
    {open && <section id="source-picker-panel" className="source-picker-panel" role="dialog"
      aria-label="切换视频" ref={panel} onKeyDown={navigate}>
      <div className="source-picker-heading">切换视频 <span>{sources.items.length} 个视频</span></div>
      {sources.items.map(item => <button type="button" className="source-picker-item" key={item.id}
        aria-current={item.id === sources.selected ? "true" : undefined} onClick={() => choose(item.id)}>
        {thumb(item)}<span className="source-picker-copy"><strong>{item.name}</strong><small>{item.detail}</small></span>
        <span className="source-picker-check" aria-hidden="true">{item.id === sources.selected ? "✓" : ""}</span>
      </button>)}
      <button className="source-picker-manage" type="button" onClick={() => {
        setOpen(false); document.getElementById("open-sources").click();
      }}>管理视频 <span aria-hidden="true">↗</span></button>
      <p className="source-picker-footnote">切换会保存当前调查，保留各自聊天记录。</p>
    </section>}
  </div>;
}

function ProjectTitle() {
  const state = useDesk();
  return <div className="project-name" data-title={state.title || "未命名调查"}>
    <input aria-label="调查名称" title="点击修改调查名称" value={state.title}
      onChange={event => store.rename(event.target.value)}
      onBlur={() => !store.getSnapshot().title.trim() && store.rename("未命名调查")}
      maxLength={80} />
  </div>;
}
function ComposerSuggestions() {
  const state = useDesk();
  const [hasDraft, setHasDraft] = useState(false);
  useEffect(() => {
    const input = document.getElementById("question");
    const read = () => setHasDraft(!!input.value.trim());
    read(); input.addEventListener("input", read);
    return () => input.removeEventListener("input", read);
  }, [state.epoch, state.sourceInfo?.id, state.title, state.records, state.selectedId]);
  const suggestions = window.FrameSeekWorkbenchModel.composerSuggestions(state);
  const shortLabels = suggestions.titles || (suggestions.label.startsWith("示例问题")
    ? ["概括可见场景", "查找清晰片段", "哪些内容无法确认？"]
    : suggestions.label === "演示示例" ? ["包裹何时在中央？", "什么时候移到右侧？", "画面里有叉车吗？"] : null);
  return <details className="composer-suggestions composer-popup" name="composer-popover" aria-label="建议问题">
    <summary title="查看建议问题，点击只填入，不会发送">建议</summary>
    <div className="composer-suggestion-panel">
      <div className="suggestion-heading"><span>{suggestions.label}</span><small>{hasDraft ? "清空草稿后可选" : "点击填入，不会发送"}</small></div>
      <div className="suggestion-buttons">{suggestions.items.map((question, index) =>
        <button key={question} type="button" title={question} disabled={hasDraft || state.local}
          onClick={(event) => {
            ui()?.fillSuggestedQuestion(question);
            event.currentTarget.closest("details").open=false;
          }}>{shortLabels?.[index] || question}</button>
      )}</div>
      {suggestions.basis && <div className="suggestion-basis">
        <button type="button" title="回看已有证据，不发送问题、不调用模型" onClick={(event) => {
          const current = store.getSnapshot();
          const source = current.records.find(item => item.id === suggestions.basis.turnId && !item.hidden && item.query.id === suggestions.basis.queryId);
          if (source && (current.sourceInfo?.id || "demo_parcel") === suggestions.basis.videoId)
            ui()?.seek(suggestions.basis.t, source.query);
          event.currentTarget.closest("details").open=false;
        }}><img src={suggestions.basis.uri} alt="" /><span>回看依据 {clock(suggestions.basis.t)}</span></button>
      </div>}
    </div>
  </details>;
}

function Outline() {
  const state = useDesk();
  const [reading, setReading] = useState(document.querySelector(".agent-shell").classList.contains("conversation-mode"));
  useEffect(() => {
    const sync = () => setReading(document.querySelector(".agent-shell").classList.contains("conversation-mode"));
    window.addEventListener("frameseek-reading-view", sync);
    return () => window.removeEventListener("frameseek-reading-view", sync);
  }, []);
  const visible = state.records.filter(r => !r.hidden);
  const hidden = state.records.filter(r => r.hidden);
  return (
    <section className="investigation-outline">
      <div className="section-line">
        <h2>{reading ? "最近的提问" : "调查脉络"}</h2>
        <span>{visible.length} 轮</span>
      </div>
      {visible.length ? (
        <ol>
          {(reading ? [...visible].reverse() : visible).map(record => (
            <li key={record.id}>
              <button
                className={record.id === state.selectedId ? "active" : ""}
                onClick={() => ui()?.select(record.id)}
              >
                <span className="outline-number">
                  {String(state.records.indexOf(record) + 1).padStart(2, "0")}
                </span>
                <span>{record.query.question}</span>
                <span
                  className={`outline-state ${record.query.response.status}`}
                />
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted-text">
          每次追问会留在画布上。选中旧问题，可以从那里继续。
        </p>
      )}
      {!!hidden.length && <details className="deleted-turns">
        <summary>已删除的问题（{hidden.length}）</summary>
        {hidden.map(record => <div key={record.id}>
          <span>{record.query.question}</span>
          <button className="text-button" onClick={() => ui()?.restoreTurn(record.id)}>恢复问题 {record.id.split("-")[1]}</button>
        </div>)}
      </details>}
      <p className="session-footnote">素材与问题独立保存 · 可继续上次调查</p>
    </section>
  );
}
function Inspector() {
  const state = useDesk();
  const tab = state.inspectorTab;
  const setTab = store.showPanel;
  const [filter, setFilter] = useState("all");
  const [order, setOrder] = useState("saved");
  const [message, setMessage] = useState("");
  useEffect(() => {
    setMessage("");
    setFilter("all");
    setOrder("saved");
  }, [state.epoch]);
  const scroll = useRef(null);
  useEffect(() => {
    scroll.current.scrollTop = 0;
    setMessage("");
  }, [state.selectedId, tab]);
  const compare = useRef(null);
  const report = useRef(null);
  const [reportText, setReportText] = useState("");
  const [reportStatus, setReportStatus] = useState("");
  const [reportData, setReportData] = useState(null);
  const [includeImages, setIncludeImages] = useState(true);
  const [exporting, setExporting] = useState(false);
  const exportController = useRef(null);
  useEffect(() => () => exportController.current?.abort(), []);
  const drag = useRef(null);
  const skipClick = useRef(false);
  const selected = state.records.find((r) => r.id === state.selectedId && !r.hidden);
  const frames = selected
    ? [
        ...new Map(
          selected.query.response.events
            .flatMap((event) => event.evidence)
            .map((frame) => [frame.uri, frame]),
        ).values(),
      ]
    : [];
  const pinnedFrames = state.pins.filter((p) => p.kind === "evidence");
  const visiblePins = window.FrameSeekWorkbenchModel.visiblePins(
    state.pins,
    filter,
    order,
  );
  const action = (fn) => {
    const result = fn();
    setMessage(
      result
        ? "已加入关键发现，保留来源关联。"
        : "暂时无法保存，发现集合最多保留 8 项。",
    );
  };
  function exportNotes() {
    const captured = window.FrameSeekReport.create(window.FrameSeekSessionUI.snapshot());
    setReportData(captured);
    setReportText(window.FrameSeekReport.markdown(captured));
    setReportStatus("");
    report.current.showModal();
  }
  function downloadFile(content, extension, type) {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `FrameSeek-调查报告.${extension}`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function downloadReport(format) {
    if (!reportData || exporting) return;
    if (format === "md") downloadFile(reportText, "md", "text/markdown;charset=utf-8");
    else if (format === "json") downloadFile(JSON.stringify(reportData, null, 2), "json", "application/json");
    else {
      const controller = new AbortController();
      exportController.current = controller;
      setExporting(true);
      const images = {};
      let bytes = 0;
      try {
        if (includeImages) for (const frame of reportData.evidence.slice(0, 24)) {
          if (!window.FrameSeekReport.safeFrameUri(reportData.source.id, frame.uri)) continue;
          setReportStatus(`正在收集证据画面 ${Object.keys(images).length} / ${reportData.evidence.length}…`);
          try {
            const response = await fetch(frame.uri, {credentials: "same-origin", signal: controller.signal});
            if (!response.ok) continue;
            const reader = response.body.getReader();
            const chunks = []; let length = 0;
            while (true) {
              const {done, value} = await reader.read();
              if (done) break;
              length += value.byteLength;
              if (length > 2 * 1024 * 1024 || bytes + length > 16 * 1024 * 1024) {
                await reader.cancel(); break;
              }
              chunks.push(value);
            }
            const mime = response.headers.get("content-type")?.split(";")[0];
            if (length > 2 * 1024 * 1024 || bytes + length > 16 * 1024 * 1024 || !["image/jpeg", "image/png", "image/webp"].includes(mime)) continue;
            const blob = new Blob(chunks, {type:mime});
            images[frame.id] = await new Promise((resolve, reject) => {
              const fileReader = new FileReader();
              fileReader.onload = () => resolve(fileReader.result);
              fileReader.onerror = reject;
              fileReader.readAsDataURL(blob);
            });
            bytes += length;
          } catch (error) { if (controller.signal.aborted) throw error; }
        }
        if (controller.signal.aborted) return;
        downloadFile(window.FrameSeekReport.html(reportData, images), "html", "text/html;charset=utf-8");
        setReportStatus(`HTML 已导出，内嵌 ${Object.keys(images).length} / ${reportData.evidence.length} 张画面。未内嵌项保留说明；文件可离线打开或打印为 PDF。`);
      } finally { setExporting(false); exportController.current = null; }
      return;
    }
    setReportStatus("下载已发起。这份报告保留打开导出窗口时的调查快照。");
  }
  return (
    <>
      <div className="inspector-heading">
        <span>
          调查检查器<small>从结论回到证据</small>
        </span>
        <button
          className="icon-button"
          aria-label="收起调查检查器"
          onClick={() => ui()?.toggleInspector()}
        >
          {icon("x")}
        </button>
      </div>
      <div
        className="inspector-tabs"
        role="tablist"
        aria-label="调查检查器内容"
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
          event.preventDefault();
          const next = tab === "context" ? "findings" : "context";
          setTab(next);
          event.currentTarget
            .querySelector(`[data-inspector-tab="${next}"]`)
            .focus();
        }}
      >
        <button
          id="inspector-context-tab"
          role="tab"
          aria-controls="inspector-panel"
          data-inspector-tab="context"
          tabIndex={tab === "context" ? 0 : -1}
          aria-selected={tab === "context"}
          onClick={() => setTab("context")}
        >
          当前问题
        </button>
        <button
          id="inspector-findings-tab"
          role="tab"
          aria-controls="inspector-panel"
          data-inspector-tab="findings"
          tabIndex={tab === "findings" ? 0 : -1}
          aria-selected={tab === "findings"}
          onClick={() => setTab("findings")}
        >
          发现 <span>{state.pins.length}</span>
        </button>
      </div>
      <div
        id="inspector-panel"
        ref={scroll}
        className="inspector-scroll"
        role="tabpanel"
        aria-label={tab === "context" ? "当前问题详情" : "已保存的发现"}
        aria-labelledby={
          tab === "context" ? "inspector-context-tab" : "inspector-findings-tab"
        }
      >
        {tab === "context" ? (
          selected ? (
            <>
              <div className="inspector-context-line">
                <p className="inspector-kicker">
                  问题{" "}
                  {String(state.records.indexOf(selected) + 1).padStart(2, "0")}
                </p>
                <span
                  className={`result-pill ${selected.query.response.status}`}
                >
                  {selected.query.engine === "index" ? "索引问答 · 未调用模型" : labels[selected.query.response.status]}
                </span>
              </div>
              <h2 className="inspector-question">{selected.query.question}</h2>
              <div className="inspector-primary-actions">
                <button
                  className="outline-button"
                  disabled={state.local}
                  onClick={() => ui()?.continueFrom(selected.id)}
                >
                  {selected.query.mode === "real" && !selected.query.response.events.length ? "重新提问" : "继续追问"} {icon("arrow-right")}
                </button>
                <button
                  className="text-button"
                  onClick={() => ui()?.select(selected.id)}
                >
                  定位画布
                </button>
                <button className="text-button danger" onClick={() => ui()?.hideTurn(selected.id)}>删除问题</button>
              </div>
              <div className="inspector-conclusion">
                <div className="section-line">
                  <h3>当前结论</h3>
                  {["ok", "partial"].includes(
                    selected.query.response.status,
                  ) && (
                    <button
                      aria-label="保存当前结论"
                      className="text-button"
                      onClick={() =>
                        action(() =>
                          ui()?.saveConclusion(selected.id, selected.query),
                        )
                      }
                    >
                      保存结论
                    </button>
                  )}
                </div>
                <p>{selected.query.response.answer}</p>
                {selected.query.engine === "index" && <button className="outline-button" onClick={()=>window.FrameSeekPersistence?.searchIndex({question:selected.query.index_search?.resolved_question || selected.query.question, answerRequestId:selected.query.id})}>查看索引时间线 · 不调用模型</button>}
                {window.FrameSeekWorkbenchModel.queryFailureReason(selected.query) &&
                  <div className="answer-review-note" role="note">
                    <strong>为什么没有回答</strong>
                    <p>{window.FrameSeekWorkbenchModel.queryFailureReason(selected.query)}</p>
                    <p>可点击「重新提问」取回原问题，编辑后自行发送；不会自动重试。</p>
                  </div>}
                {window.FrameSeekWorkbenchModel.answerReviewNotes(selected.query.response.answer).map(note =>
                  <div className="answer-review-note" key={note} role="note">
                    <strong>数字变化需要复核</strong><p>{note}</p>
                    {!!frames.length && <button type="button" onClick={() => ui()?.seek(frames[0].t, selected.query)}>回看对应证据 {clock(frames[0].t)}</button>}
                  </div>
                )}
              </div>
              {!!selected.query.response.unresolved.length && (
                <div className="open-questions">
                  <h3>仍需核实</h3>
                  {selected.query.response.unresolved.map((text) => (
                    <p key={text}>{text}</p>
                  ))}
                </div>
              )}
              {!!frames.length && (
                <section className="inspector-evidence">
                  <div className="section-line">
                    <h3>引用画面</h3>
                    <span>{frames.length}</span>
                  </div>
                  {frames.map((frame) => (
                    <div className="frame-row" key={frame.uri}>
                      <button
                        className="frame-preview"
                        draggable={false}
                        data-drag-evidence
                        title="拖到画布保存，或点击回看"
                        onPointerDown={(event) => {
                          if (event.button !== 0) return;
                          event.preventDefault();
                          event.currentTarget.focus();
                          event.currentTarget.setPointerCapture(
                            event.pointerId,
                          );
                          drag.current = {
                            x: event.clientX,
                            y: event.clientY,
                            moved: false,
                          };
                          skipClick.current = false;
                        }}
                        onPointerMove={(event) => {
                          if (!drag.current) return;
                          if (
                            Math.hypot(
                              event.clientX - drag.current.x,
                              event.clientY - drag.current.y,
                            ) > 8
                          )
                            drag.current.moved = true;
                          if (drag.current.moved)
                            document
                              .getElementById("chat-content")
                              .classList.add("drop-ready");
                        }}
                        onPointerUp={(event) => {
                          const gesture = drag.current;
                          drag.current = null;
                          document
                            .getElementById("chat-content")
                            .classList.remove("drop-ready");
                          if (!gesture?.moved) return;
                          skipClick.current = true;
                          const rect = document
                            .getElementById("chat-content")
                            .getBoundingClientRect();
                          if (
                            event.clientX >= rect.left &&
                            event.clientX < rect.right &&
                            event.clientY >= rect.top &&
                            event.clientY < rect.bottom
                          )
                            action(() =>
                              ui()?.saveEvidence(
                                selected.id,
                                selected.query,
                                frame,
                              ),
                            );
                        }}
                        onPointerCancel={() => {
                          drag.current = null;
                          document
                            .getElementById("chat-content")
                            .classList.remove("drop-ready");
                        }}
                        onClick={() => {
                          if (skipClick.current) {
                            skipClick.current = false;
                            return;
                          }
                          ui()?.seek(frame.t, selected.query);
                        }}
                        aria-label={`回看引用 ${clock(frame.t)}`}
                      >
                        <img src={frame.uri} alt={frame.caption} loading="lazy" decoding="async" />
                        <span>{clock(frame.t)}</span>
                      </button>
                      <div>
                        <p>{frame.caption}</p>
                        <button
                          className="text-button"
                          onClick={() =>
                            action(() =>
                              ui()?.saveEvidence(
                                selected.id,
                                selected.query,
                                frame,
                              ),
                            )
                          }
                          aria-label={`保存证据 ${clock(frame.t)}`}
                        >
                          留在画布 {icon("plus")}
                        </button>
                      </div>
                    </div>
                  ))}
                </section>
              )}
              <details className="basis-section">
                <summary>
                  这轮依据<span>{frames.length} 张证据 · {selected.query.mode === "real" ? (frames.length ? "画面证据" : "未生成有效证据") : "预设匹配"}</span>
                </summary>
                <ol className="basis-steps">
                  <li>
                    <span>01</span>
                    <div>
                      原视频范围<small>{state.sourceInfo?.name || "合成视频"} · 00:00–{clock(state.sourceInfo?.metadata?.duration || 18)}</small>
                    </div>
                  </li>
                  <li>
                    <span>02</span>
                    <div>
                      {selected.query.mode === "real" ? "证据检查" : "固定样例匹配"}
                      <small>
                        {selected.query.response.events.length} {['local','index'].includes(selected.query.engine) ? '组匹配画面' : '段事件'} ·{" "}
                        {frames.length} 张证据帧
                      </small>
                    </div>
                  </li>
                  <li>
                    <span>03</span>
                    <div>
                      {labels[selected.query.response.status]}
                      <small>{selected.query.engine === "index" ? "已检索索引 · 未新增视觉复核" : selected.query.mode === "real" ? `${selected.query.response.trace.visible.segments_reviewed} 个片段已回看` : "未执行新的模型推理"}</small>
                    </div>
                  </li>
                </ol>
              </details>
            </>
          ) : (
            <div className="inspector-empty">
              <p className="inspector-kicker">FOLLOW THE EVIDENCE</p>
              <h2>让结论有据可查。</h2>
              <p>选中画布中的问题，在这里查看它的依据、证据和未确认项。</p>
              <button className="outline-button" onClick={() => ui()?.demos()}>
                探索演示路径 {icon("arrow-right")}
              </button>
            </div>
          )
        ) : (
          <>
            <label className="inspector-kicker" htmlFor="finding-group">
              发现分组
            </label>
            <input
              id="finding-group"
              className="finding-group"
              value={state.groupTitle}
              onChange={(e) => store.group(e.target.value)}
              maxLength={60}
            />
            <div className="collection-summary">
              <span>{state.pins.length} / 8 项已保存</span>
              <span>
                {pinnedFrames.length} 张证据 ·{" "}
                {state.pins.length - pinnedFrames.length} 个结论
              </span>
            </div>
            <div className="finding-controls">
              <div
                className="finding-filters"
                role="group"
                aria-label="发现类型筛选"
              >
                {[
                  ["all", "全部"],
                  ["evidence", "证据"],
                  ["conclusion", "结论"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    aria-label={`筛选${label}发现`}
                    aria-pressed={filter === value}
                    onClick={() => setFilter(value)}
                  >
                    {label}
                    <span>
                      {value === "all"
                        ? state.pins.length
                        : state.pins.filter((p) => p.kind === value).length}
                    </span>
                  </button>
                ))}
              </div>
              <label className="sr-only" htmlFor="finding-order">
                发现排序
              </label>
              <select
                id="finding-order"
                value={order}
                onChange={(e) => setOrder(e.target.value)}
              >
                <option value="saved">保存顺序</option>
                <option value="time">视频时间</option>
              </select>
            </div>
            {visiblePins.length ? (
              <div className="finding-list">
                {visiblePins.map((pin) => (
                  <div className={`finding-row ${pin.kind}`} key={pin.id}>
                    <button
                      className="finding-main"
                      aria-label={`定位发现：${pin.title}`}
                      onClick={() => ui()?.focusFinding(pin.id)}
                    >
                      {pin.kind === "evidence" ? (
                        <img src={pin.frame.uri} alt={pin.frame.caption} />
                      ) : (
                        icon("chats-circle")
                      )}
                      <span>
                        <small>
                          {pin.kind === "evidence" ? "证据帧" : "结论"} /{" "}
                          {pin.kind === "evidence"
                            ? clock(pin.frame.t)
                            : labels[pin.query.response.status]}
                        </small>
                        <span className="finding-caption">
                          {pin.kind === "evidence"
                            ? pin.frame.caption
                            : pin.query.response.answer}
                        </span>
                      </span>
                    </button>
                    <div className="finding-provenance">
                      <button
                        className="text-button"
                        aria-label={`查看发现来源：${pin.title}`}
                        disabled={
                          !state.records.some(
                            (record) => record.id === pin.turnId,
                          )
                        }
                        onClick={() => {
                          ui()?.select(pin.turnId);
                          setTab("context");
                          document
                            .getElementById("inspector-context-tab")
                            .focus();
                        }}
                      >
                        来源：{pin.query.question}
                      </button>
                      {pin.kind === "evidence" && (
                        <button
                          className="text-button"
                          aria-label={`回看已保存证据 ${clock(pin.frame.t)}`}
                          onClick={() => ui()?.seek(pin.frame.t, pin.query)}
                        >
                          {icon("play")}回看
                        </button>
                      )}
                    </div>
                    <button
                      className="icon-button remove-finding"
                      aria-label={`移除${pin.title}`}
                      onClick={() => ui()?.removeFinding(pin.id)}
                    >
                      {icon("x")}
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="muted-text">
                {state.pins.length
                  ? "这个分类还没有发现。切换到全部可查看已保存内容。"
                  : "把引用画面或当前结论保存到这里。它们会作为独立对象留在画布上。"}
              </p>
            )}
            <button
              className="outline-button compare-button"
              disabled={pinnedFrames.length < 2}
              onClick={() => compare.current.showModal()}
            >
              并排核对两帧{" "}
              <span>
                {pinnedFrames.length < 2
                  ? "需 2 张证据"
                  : `${pinnedFrames.length} 张可用`}
              </span>
            </button>
            <label
              className="section-line note-label"
              htmlFor="investigation-note"
            >
              调查笔记 <span>{state.note.length} / 1200</span>
            </label>
            <textarea
              id="investigation-note"
              aria-label="调查笔记"
              placeholder="记录观察、疑问或下一步…"
              value={state.note}
              onChange={(e) => store.annotate(e.target.value)}
              maxLength={1200}
            />
            <p className="session-footnote">
              名称、发现和笔记自动保存在本机，刷新后可继续。
            </p>
            <button className="export-button" onClick={exportNotes}>
              导出调查记录 {icon("arrow-right")}
            </button>
          </>
        )}
        <p className="inspector-message" role="status">
          {message}
        </p>
      </div>
      <dialog
        className="compare-dialog"
        ref={compare}
        aria-labelledby="compare-title"
      >
        <header>
          <div>
            <p className="inspector-kicker">EVIDENCE / COMPARISON</p>
            <h2 id="compare-title">并排核对</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭证据对照"
            onClick={() => compare.current.close()}
          >
            {icon("x")}
          </button>
        </header>
        <div className="compare-frames">
          {pinnedFrames.slice(0, 2).map((pin) => (
            <figure key={pin.id}>
              <img src={pin.frame.uri} alt={pin.frame.caption} />
              <figcaption>
                <strong>{clock(pin.frame.t)}</strong>
                <span>{pin.frame.caption}</span>
                <button
                  className="text-button"
                  onClick={() => {
                    compare.current.close();
                    ui()?.seek(pin.frame.t, pin.query);
                  }}
                >
                  回看此帧 {icon("play")}
                </button>
              </figcaption>
            </figure>
          ))}
        </div>
        <p className="muted-text">
          同一素材的证据帧。可见位置变化不等于已确认连续移动。
        </p>
      </dialog>
      <dialog
        className="report-dialog compare-dialog"
        ref={report}
        aria-labelledby="report-title"
        onClose={() => exportController.current?.abort()}
      >
        <header>
          <div>
            <p className="inspector-kicker">INVESTIGATION / RECORD</p>
            <h2 id="report-title">调查报告</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭调查记录"
            onClick={() => report.current.close()}
          >
            {icon("x")}
          </button>
        </header>
        <p className="report-summary">{reportData?.questions.length || 0} 个问题 · {reportData?.findings.length || 0} 项发现 · {reportData?.evidence.length || 0} 张证据画面。包含当前保留的对话与发现来源；未确认项单独列出。</p>
        <textarea aria-label="调查记录 Markdown" value={reportText} readOnly />
        <label className="report-image-option"><input type="checkbox" checked={includeImages} onChange={e => setIncludeImages(e.target.checked)} disabled={exporting} /> HTML 内嵌证据画面（最多 24 张、16 MB，形成可分享的本地副本）</label>
        <div className="report-actions">
          <button
            className="outline-button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(reportText);
                setReportStatus("Markdown 已复制。");
              } catch {
                setReportStatus("可在上方选择文字，使用浏览器复制。");
              }
            }}
          >
            复制 Markdown
          </button>
          <button className="outline-button" disabled={exporting} onClick={() => downloadReport("md")}>下载 Markdown</button>
          <button className="outline-button" disabled={exporting} onClick={() => downloadReport("json")}>下载 JSON</button>
          <button className="outline-button" disabled={exporting} onClick={() => downloadReport("html").catch(() => setReportStatus("导出已取消或失败，请重试。"))}>{exporting ? "收集画面…" : "下载 HTML"}</button>
        </div>
        <p className="muted-text" role="status">
          {reportStatus}
        </p>
      </dialog>
    </>
  );
}
createRoot(document.getElementById("workspace-chrome")).render(
  <WorkspaceChrome />,
);
createRoot(document.getElementById("composer-suggestions")).render(<ComposerSuggestions />);
createRoot(document.getElementById("source-picker-root")).render(<SourcePicker />);
createRoot(document.getElementById("project-title")).render(<ProjectTitle />);
createRoot(document.getElementById("investigation-outline")).render(
  <Outline />,
);
createRoot(document.getElementById("workbench-inspector")).render(
  <Inspector />,
);

createRoot(document.getElementById("workspace-view-tabs")).render(<WorkspaceView />);
createRoot(document.getElementById("conversation-context-chip")).render(<ConversationContext />);
createRoot(document.getElementById("conversation-root")).render(<Conversation />);
