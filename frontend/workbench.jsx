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
const displayText = window.FrameSeekWorkbenchModel.displayText;
const useDesk = () => useSyncExternalStore(store.subscribe, store.getSnapshot);
const ui = () => window.FrameSeekUI;
const icon = (name) => (
  <img className="icon" src={`/static/icons/${name}.svg`} alt="" />
);
const labels = {
  ok: "Evidence found",
  partial: "Partially confirmed",
  no_result: "No matching frames",
  error: "Incomplete",
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
    document.getElementById("chat-content").setAttribute("aria-label", mode === "conversation" ? "Video conversation" : "Investigation canvas");
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
  return <div className="reading-tabs" role="group" aria-label="Workspace view">
    <button type="button" aria-pressed={mode === "conversation"} onClick={() => setMode("conversation")}>Conversation</button>
    <button type="button" aria-pressed={mode === "canvas"} onClick={() => setMode("canvas")}>Canvas</button>
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
    if (!input.disabled) input.placeholder = context ? "Ask a follow-up…" : "Ask a question about this video…";
  }, [context?.heading, context?.answer, state.sourceInfo?.id]);
  return context ? <div className="reading-context-chip" role="group" aria-label={context.heading}>
    <div className="reading-quote-copy">
      <strong>{context.heading}</strong>
      <p className="reading-quote-question" title={context.question}>{context.question}</p>
      <p className="reading-quote-answer" title={context.answer}>Answer: {context.answer}</p>
    </div>
    <button type="button" aria-label="Remove reference, keep draft" onClick={() => document.getElementById("clear-context").click()}>Remove reference</button>
  </div> : null;
}
const ConversationTurn = memo(function ConversationTurn({record, selected, ocrAvailable}) {
  const [expanded, setExpanded] = useState(false);
  const [checkedExpanded, setCheckedExpanded] = useState(false);
  const query = record.query, response = query.response;
  const presentation = window.FrameSeekWorkbenchModel.conversationAnswer(query);
  const frames = expanded ? presentation.frames : presentation.frames.slice(0,2);
  const replay = frame => { ui()?.seek(frame.t, query); };
  return <article id={`conversation-${record.id}`} className={`conversation-turn${selected ? " is-selected" : ""}`} aria-label={`Question and answer: ${displayText(query.question, query)}`}>
    <div className="reading-question">{icon("chats-circle")}<p>{displayText(query.question, query)}</p></div>
    <div className="reading-answer">
      <span className="reading-mark">{icon("chats-circle")}</span>
      <span className="reading-speaker">FrameSeek <span>{presentation.label}</span></span>
      <p className="reading-answer-text">{presentation.prefix}{presentation.text}</p>
      {presentation.caution && <p className="reading-caution">{presentation.caution}</p>}
      {!!frames.length && <div className="reading-evidence-grid">{frames.map(frame => <button className="reading-evidence" type="button" key={frame.frame_id || frame.uri}
        aria-label={`Replay evidence ${clock(frame.t)}`} onClick={() => replay(frame)}>
        <img src={frame.uri} alt={presentation.englishDemo ? "Synthetic parcel demo evidence" : frame.caption || "Cited source frame"} loading="lazy" decoding="async"/>
        <span>{icon("play")}<time>{clock(frame.t)}</time><strong>Replay</strong></span>
      </button>)}</div>}
      {!!presentation.frames.length && <p className="reading-evidence-note">{presentation.frames.length} {presentation.frames.length === 1 ? "cited frame" : "cited frames"} · {presentation.evidenceNote}</p>}
      {query.mode !== "real" && query.video_id === "demo_basketball" && !!response.followups?.length && <div className="reading-demo-examples">
        <small>Next: choose a follow-up, then send it.</small>
        <div className="suggestion-buttons">{response.followups.map(question => <button key={question} type="button" onClick={() => ui()?.fillSuggestedQuestion(question, record.id)}>{question}</button>)}</div>
      </div>}
      {presentation.frames.length > 2 && <button type="button" className="text-button reading-show-all" onClick={() => setExpanded(!expanded)}>{expanded ? "Show fewer frames" : `Show all ${presentation.frames.length} frames`}</button>}
      {!!presentation.checkedFrames.length && <>
        <p className="reading-evidence-note">Sampled frames from this search · not confirmed matches. Click to review the source video.</p>
        <div className="reading-evidence-grid">{(checkedExpanded ? presentation.checkedFrames : presentation.checkedFrames.slice(0,2)).map(frame =>
          <button className="reading-evidence" type="button" key={frame.frame_id} aria-label={`Review sampled frame ${clock(frame.t)} · Non-matching evidence`} onClick={() => replay(frame)}>
            <img src={frame.uri} alt="Reviewed sample frame; not a confirmed match" loading="lazy" decoding="async"/>
            <span>{icon("play")}<time>{clock(frame.t)}</time><strong>Replay</strong></span>
          </button>)}</div>
        {presentation.checkedFrames.length > 2 && <button type="button" className="text-button reading-show-all" onClick={() => setCheckedExpanded(!checkedExpanded)}>{checkedExpanded ? "Show fewer sampled frames" : `View all ${presentation.checkedFrames.length} sampled frames`}</button>}
      </>}
      <div className="reading-response-actions">
        <button type="button" className="text-button" onClick={() => ui()?.continueFrom(record.id)}>{query.mode === "real" && !response.events.length ? "Edit question" : "Ask a follow-up"}</button>
        {query.engine === "index" && <button type="button" className="text-button" onClick={() => window.FrameSeekPersistence?.searchIndex({question: query.index_search?.resolved_question || query.question, answerRequestId: query.id})}>View index and candidate screens</button>}
        {window.FrameSeekWorkbenchModel.shouldOfferTextCandidates(query, ocrAvailable, window.FrameSeekPersistence?.canSearchOcr) && <button type="button" className="text-button" onClick={() => window.FrameSeekPersistence?.searchIndex({question: query.index_search?.resolved_question || query.question, answerRequestId: query.id, autoOcr: true})}>Find more text candidates · No model calls</button>}
        <details className="reading-basis"><summary>Evidence and actions</summary>
          <p>{response.events.length} frame sets · {presentation.frames.length} references</p>
          {(response.unresolved || []).map((text,index) => <p key={index}>{displayText(text, query)}</p>)}
          {response.status === "error" && <p>{window.FrameSeekWorkbenchModel.queryFailureReason(query)}</p>}
          <button type="button" className="text-button" onClick={() => ui()?.saveConclusion(record.id,query)}>Save conclusion</button>
          <button type="button" className="text-button danger" onClick={() => ui()?.hideTurn(record.id)}>Delete question</button>
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
  return <div className="conversation-stream" role="region" aria-label="Questions, answers and video evidence">
    {!records.length && <div className="reading-empty">
      <div className="reading-empty-mark" aria-hidden="true"><span></span><span></span><span></span></div>
      <p className="reading-eyebrow">Video investigation / Conversation</p>
      <h1>{state.local ? "Video loaded." : "Start with a question."}</h1>
      <p className="reading-empty-description">{state.local ? "You can play the video now. Ask questions and review frames when the source is ready." : "Ask a question, review the footage, and follow the evidence."}</p>
      {state.local && <div className="reading-empty-status"><span className="reading-status-dot" aria-hidden="true"></span><div><strong>Questions unavailable for now</strong><p>Check the source status below the player for details.</p></div></div>}
      {!state.local && <div className="reading-empty-steps" aria-label="Investigation steps"><span>01 Ask</span><span>02 Review</span><span>03 Follow up</span></div>}
      {["demo_parcel", "demo_basketball"].includes(state.sourceInfo?.id) && !state.local && <div className="reading-demo-examples">
        <p>{state.sourceInfo?.id === "demo_basketball" ? "Synthetic basketball demo" : "Synthetic parcel demo"} · Preset answers · No model calls</p>
        <div className="suggestion-buttons">{window.FrameSeekWorkbenchModel.composerSuggestions(state).items.map(question =>
          <button key={question} type="button" onClick={() => ui()?.fillSuggestedQuestion(question)}>{question}</button>)}</div>
        <small>Choose a question, then send it. Replay a frame and ask a follow-up.</small>
      </div>}
      {state.sourceInfo?.id !== "demo_basketball" && <button type="button" className="text-button" onClick={() => window.FrameSeekPersistence?.openDemo()}>Start basketball demo</button>}
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
      aria-label={`Switch video: ${current?.name || "Loading"}`} aria-haspopup="dialog"
      aria-expanded={open} aria-controls="source-picker-panel" disabled={sources.busy}
      onClick={() => setOpen(value => !value)} onKeyDown={event => {
        if (event.key === "ArrowDown") {event.preventDefault(); setOpen(true);}
      }}>
      {thumb(current)}
      <span className="source-picker-copy"><small>{sources.busy ? "Switching…" : "Current video"}</small>
        <strong>{current?.name || "Loading video…"}</strong></span>
      <span className="source-picker-chevron" aria-hidden="true">⌄</span>
    </button>
    {open && <section id="source-picker-panel" className="source-picker-panel" role="dialog"
      aria-label="Switch video" ref={panel} onKeyDown={navigate}>
      <div className="source-picker-heading">Switch video <span>{sources.items.length} videos</span></div>
      {sources.items.map(item => <button type="button" className="source-picker-item" key={item.id}
        aria-current={item.id === sources.selected ? "true" : undefined} onClick={() => choose(item.id)}>
        {thumb(item)}<span className="source-picker-copy"><strong>{item.name}</strong><small>{item.detail}</small></span>
        <span className="source-picker-check" aria-hidden="true">{item.id === sources.selected ? "✓" : ""}</span>
      </button>)}
      <button className="source-picker-manage" type="button" onClick={() => {
        setOpen(false); document.getElementById("open-sources").click();
      }}>Manage videos <span aria-hidden="true">↗</span></button>
      <p className="source-picker-footnote">Switching saves the current investigation and keeps each conversation.</p>
    </section>}
  </div>;
}

function ProjectTitle() {
  const state = useDesk();
  return <div className="project-name" data-title={state.title || "Untitled investigation"}>
    <input aria-label="Investigation name" title="Click to rename investigation" value={state.title}
      onChange={event => store.rename(event.target.value)}
      onBlur={() => !store.getSnapshot().title.trim() && store.rename("Untitled investigation")}
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
  const shortLabels = suggestions.titles || (suggestions.label.startsWith("Example questions")
    ? ["Summarize the scene", "Find a clear moment", "What is unclear?"]
    : suggestions.label === "Demo questions" && state.sourceInfo?.id === "demo_parcel" ? ["When is the parcel centered?", "When is it on the right?", "Is there a forklift?"] : null);
  return <details className="composer-suggestions composer-popup" name="composer-popover" aria-label="Suggested questions">
    <summary title="Choose a suggestion to fill the question box; it will not be sent automatically.">Suggestions</summary>
    <div className="composer-suggestion-panel">
      <div className="suggestion-heading"><span>{suggestions.label}</span><small>{hasDraft ? "Optional after clearing draft" : "Click to fill in; it will not send"}</small></div>
      <div className="suggestion-buttons">{suggestions.items.map((question, index) =>
        <button key={question} type="button" title={question} disabled={hasDraft || state.local}
          onClick={(event) => {
            ui()?.fillSuggestedQuestion(question);
            event.currentTarget.closest("details").open=false;
          }}>{shortLabels?.[index] || question}</button>
      )}</div>
      {suggestions.basis && <div className="suggestion-basis">
        <button type="button" title="Review saved evidence without sending a question or calling a model" onClick={(event) => {
          const current = store.getSnapshot();
          const source = current.records.find(item => item.id === suggestions.basis.turnId && !item.hidden && item.query.id === suggestions.basis.queryId);
          if (source && (current.sourceInfo?.id || "demo_parcel") === suggestions.basis.videoId)
            ui()?.seek(suggestions.basis.t, source.query);
          event.currentTarget.closest("details").open=false;
        }}><img src={suggestions.basis.uri} alt="" /><span>Review evidence {clock(suggestions.basis.t)}</span></button>
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
        <h2>{reading ? "Recent questions" : "Investigation outline"}</h2>
        <span>{visible.length} turns</span>
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
                <span>{displayText(record.query.question, record.query)}</span>
                <span
                  className={`outline-state ${record.query.response.status}`}
                />
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted-text">
          Follow-ups remain on the canvas. Select an earlier question to continue.
        </p>
      )}
      {!!hidden.length && <details className="deleted-turns">
        <summary>Deleted questions ({hidden.length})</summary>
        {hidden.map(record => <div key={record.id}>
          <span>{displayText(record.query.question, record.query)}</span>
          <button className="text-button" onClick={() => ui()?.restoreTurn(record.id)}>Restore question {record.id.split("-")[1]}</button>
        </div>)}
      </details>}
      <p className="session-footnote">Videos and questions are saved separately · Continue your investigation later</p>
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
        ? "Added to findings with its source."
        : "Could not save; findings are limited to 8 items.",
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
    anchor.download = `FrameSeek-investigation-report.${extension}`;
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
          setReportStatus(`Collecting evidence ${Object.keys(images).length} / ${reportData.evidence.length}…`);
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
        setReportStatus(`HTML exported, embedded ${Object.keys(images).length} / ${reportData.evidence.length} picture. Notes are retained for items not embedded; files can be opened offline or printed to PDF.`);
      } finally { setExporting(false); exportController.current = null; }
      return;
    }
    setReportStatus("Download initiated. This report retains a snapshot of the investigation when the export window was opened.");
  }
  return (
    <>
      <div className="inspector-heading">
        <span>
          Investigation inspector<small>From conclusion to evidence</small>
        </span>
        <button
          className="icon-button"
          aria-label="Collapse investigation inspector"
          onClick={() => ui()?.toggleInspector()}
        >
          {icon("x")}
        </button>
      </div>
      <div
        className="inspector-tabs"
        role="tablist"
        aria-label="Investigation inspector content"
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
          Current question
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
          Findings <span>{state.pins.length}</span>
        </button>
      </div>
      <div
        id="inspector-panel"
        ref={scroll}
        className="inspector-scroll"
        role="tabpanel"
        aria-label={tab === "context" ? "Current question details" : "Saved findings"}
        aria-labelledby={
          tab === "context" ? "inspector-context-tab" : "inspector-findings-tab"
        }
      >
        {tab === "context" ? (
          selected ? (
            <>
              <div className="inspector-context-line">
                <p className="inspector-kicker">
                  Question{" "}
                  {String(state.records.indexOf(selected) + 1).padStart(2, "0")}
                </p>
                <span
                  className={`result-pill ${selected.query.response.status}`}
                >
                  {selected.query.engine === "index" ? "Index Q&A · Model not called" : labels[selected.query.response.status]}
                </span>
              </div>
              <h2 className="inspector-question">{displayText(selected.query.question, selected.query)}</h2>
              <div className="inspector-primary-actions">
                <button
                  className="outline-button"
                  disabled={state.local}
                  onClick={() => ui()?.continueFrom(selected.id)}
                >
                  {selected.query.mode === "real" && !selected.query.response.events.length ? "Ask again" : "Keep asking"} {icon("arrow-right")}
                </button>
                <button
                  className="text-button"
                  onClick={() => ui()?.select(selected.id)}
                >
                  Position canvas
                </button>
                <button className="text-button danger" onClick={() => ui()?.hideTurn(selected.id)}>Delete question</button>
              </div>
              <div className="inspector-conclusion">
                <div className="section-line">
                  <h3>Current conclusion</h3>
                  {["ok", "partial"].includes(
                    selected.query.response.status,
                  ) && (
                    <button
                      aria-label="Save current conclusion"
                      className="text-button"
                      onClick={() =>
                        action(() =>
                          ui()?.saveConclusion(selected.id, selected.query),
                        )
                      }
                    >
                      Save conclusion
                    </button>
                  )}
                </div>
                <p>{displayText(selected.query.response.answer, selected.query)}</p>
                {selected.query.engine === "index" && <button className="outline-button" onClick={()=>window.FrameSeekPersistence?.searchIndex({question:selected.query.index_search?.resolved_question || selected.query.question, answerRequestId:selected.query.id})}>View index timeline · No model calls</button>}
                {window.FrameSeekWorkbenchModel.queryFailureReason(selected.query) &&
                  <div className="answer-review-note" role="note">
                    <strong>why no answer</strong>
                    <p>{window.FrameSeekWorkbenchModel.queryFailureReason(selected.query)}</p>
                    <p>You can click "Reask" to retrieve the original question, edit it and send it yourself; it will not be automatically retried.</p>
                  </div>}
                {window.FrameSeekWorkbenchModel.answerReviewNotes(selected.query.response.answer).map(note =>
                  <div className="answer-review-note" key={note} role="note">
                    <strong>Verify numerical changes</strong><p>{note}</p>
                    {!!frames.length && <button type="button" onClick={() => ui()?.seek(frames[0].t, selected.query)}>Look back at the corresponding evidence {clock(frames[0].t)}</button>}
                  </div>
                )}
              </div>
              {!!selected.query.response.unresolved.length && (
                <div className="open-questions">
                  <h3>Still need to verify</h3>
                  {selected.query.response.unresolved.map((text) => (
                    <p key={text}>{displayText(text, selected.query)}</p>
                  ))}
                </div>
              )}
              {!!frames.length && (
                <section className="inspector-evidence">
                  <div className="section-line">
                    <h3>Referenced frame</h3>
                    <span>{frames.length}</span>
                  </div>
                  {frames.map((frame) => (
                    <div className="frame-row" key={frame.uri}>
                      <button
                        className="frame-preview"
                        draggable={false}
                        data-drag-evidence
                        title="Drag to save on the canvas, or click to replay"
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
                        aria-label={`Replay evidence at ${clock(frame.t)}`}
                      >
                        <img src={frame.uri} alt={displayText(frame.caption, selected.query)} loading="lazy" decoding="async" />
                        <span>{clock(frame.t)}</span>
                      </button>
                      <div>
                        <p>{displayText(frame.caption, selected.query)}</p>
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
                          aria-label={`Save evidence at ${clock(frame.t)}`}
                        >
                          Save to canvas {icon("plus")}
                        </button>
                      </div>
                    </div>
                  ))}
                </section>
              )}
              <details className="basis-section">
                <summary>
                  Sources for this answer<span>{frames.length} evidence frames · {selected.query.mode === "real" ? (frames.length ? "Video evidence" : "No valid evidence generated") : "Preset match"}</span>
                </summary>
                <ol className="basis-steps">
                  <li>
                    <span>01</span>
                    <div>
                      Source video range<small>{state.sourceInfo?.name || "Synthetic video"} · 00:00–{clock(state.sourceInfo?.metadata?.duration || 18)}</small>
                    </div>
                  </li>
                  <li>
                    <span>02</span>
                    <div>
                      {selected.query.mode === "real" ? "evidence examination" : "Preset example match"}
                      <small>
                        {selected.query.response.events.length} {['local','index'].includes(selected.query.engine) ? "matching frame sets" : "events"} ·{" "}
                        {frames.length} evidence frames
                      </small>
                    </div>
                  </li>
                  <li>
                    <span>03</span>
                    <div>
                      {labels[selected.query.response.status]}
                      <small>{selected.query.engine === "index" ? "Searched index · No new visual review added" : selected.query.mode === "real" ? `${selected.query.response.trace.visible.segments_reviewed} clips have been viewed` : "New model inference is not performed"}</small>
                    </div>
                  </li>
                </ol>
              </details>
            </>
          ) : (
            <div className="inspector-empty">
              <p className="inspector-kicker">FOLLOW THE EVIDENCE</p>
              <h2>Keep conclusions grounded in evidence.</h2>
              <p>Select an issue in the canvas to view its rationale, evidence, and unconfirmed items here.</p>
              <button className="outline-button" onClick={() => ui()?.demos()}>
                Explore the demo path {icon("arrow-right")}
              </button>
            </div>
          )
        ) : (
          <>
            <label className="inspector-kicker" htmlFor="finding-group">
              Group findings
            </label>
            <input
              id="finding-group"
              className="finding-group"
              value={state.groupTitle}
              onChange={(e) => store.group(e.target.value)}
              maxLength={60}
            />
            <div className="collection-summary">
              <span>{state.pins.length} / 8 items saved</span>
              <span>
                {pinnedFrames.length} evidence frames ·{" "}
                {state.pins.length - pinnedFrames.length} conclusions
              </span>
            </div>
            <div className="finding-controls">
              <div
                className="finding-filters"
                role="group"
                aria-label="Filter findings by type"
              >
                {[
                  ["all", "All"],
                  ["evidence", "Evidence"],
                  ["conclusion", "Conclusions"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    aria-label={`Filter findings: ${label}`}
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
                Sort findings
              </label>
              <select
                id="finding-order"
                value={order}
                onChange={(e) => setOrder(e.target.value)}
              >
                <option value="saved">Save order</option>
                <option value="time">video time</option>
              </select>
            </div>
            {visiblePins.length ? (
              <div className="finding-list">
                {visiblePins.map((pin) => (
                  <div className={`finding-row ${pin.kind}`} key={pin.id}>
                    <button
                      className="finding-main"
                      aria-label={`Location discovery:${pin.title}`}
                      onClick={() => ui()?.focusFinding(pin.id)}
                    >
                      {pin.kind === "evidence" ? (
                        <img src={pin.frame.uri} alt={displayText(pin.frame.caption, pin.query)} />
                      ) : (
                        icon("chats-circle")
                      )}
                      <span>
                        <small>
                          {pin.kind === "evidence" ? "Evidence frame" : "Conclusion"} /{" "}
                          {pin.kind === "evidence"
                            ? clock(pin.frame.t)
                            : labels[pin.query.response.status]}
                        </small>
                        <span className="finding-caption">
                          {pin.kind === "evidence"
                            ? displayText(pin.frame.caption, pin.query)
                            : displayText(pin.query.response.answer, pin.query)}
                        </span>
                      </span>
                    </button>
                    <div className="finding-provenance">
                      <button
                        className="text-button"
                        aria-label={`View discovery sources:${pin.title}`}
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
                        Source: {displayText(pin.query.question, pin.query)}
                      </button>
                      {pin.kind === "evidence" && (
                        <button
                          className="text-button"
                          aria-label={`Review saved evidence ${clock(pin.frame.t)}`}
                          onClick={() => ui()?.seek(pin.frame.t, pin.query)}
                        >
                          {icon("play")}Replay
                        </button>
                      )}
                    </div>
                    <button
                      className="icon-button remove-finding"
                      aria-label={`Remove${pin.title}`}
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
                  ? "This category has not been found yet. Switch to All to view saved content."
                  : "Save referenced images or current conclusions here. They remain on the canvas as independent objects."}
              </p>
            )}
            <button
              className="outline-button compare-button"
              disabled={pinnedFrames.length < 2}
              onClick={() => compare.current.showModal()}
            >
              Check two frames side by side{" "}
              <span>
                {pinnedFrames.length < 2
                  ? "2 pieces of evidence required"
                  : `${pinnedFrames.length} Zhang Keke`}
              </span>
            </button>
            <label
              className="section-line note-label"
              htmlFor="investigation-note"
            >
              Investigation Notes <span>{state.note.length} / 1200</span>
            </label>
            <textarea
              id="investigation-note"
              aria-label="Investigation Notes"
              placeholder="Record observations, questions, or next steps…"
              value={state.note}
              onChange={(e) => store.annotate(e.target.value)}
              maxLength={1200}
            />
            <p className="session-footnote">
              Names, findings and notes are automatically saved locally and can be continued after refreshing.
            </p>
            <button className="export-button" onClick={exportNotes}>
              Export investigation records {icon("arrow-right")}
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
            <h2 id="compare-title">Check side by side</h2>
          </div>
          <button
            className="icon-button"
            aria-label="Close evidence comparison"
            onClick={() => compare.current.close()}
          >
            {icon("x")}
          </button>
        </header>
        <div className="compare-frames">
          {pinnedFrames.slice(0, 2).map((pin) => (
            <figure key={pin.id}>
              <img src={pin.frame.uri} alt={displayText(pin.frame.caption, pin.query)} />
              <figcaption>
                <strong>{clock(pin.frame.t)}</strong>
                <span>{displayText(pin.frame.caption, pin.query)}</span>
                <button
                  className="text-button"
                  onClick={() => {
                    compare.current.close();
                    ui()?.seek(pin.frame.t, pin.query);
                  }}
                >
                  Look back at this frame {icon("play")}
                </button>
              </figcaption>
            </figure>
          ))}
        </div>
        <p className="muted-text">
          Evidence frames of the same footage. Visible position changes do not equal confirmed continuous movement.
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
            <h2 id="report-title">investigation report</h2>
          </div>
          <button
            className="icon-button"
            aria-label="Close investigation report"
            onClick={() => report.current.close()}
          >
            {icon("x")}
          </button>
        </header>
        <p className="report-summary">{reportData?.questions.length || 0} questions · {reportData?.findings.length || 0} findings · {reportData?.evidence.length || 0} evidence frames. Includes saved conversations and finding sources; unconfirmed items are listed separately.</p>
        <textarea aria-label="Investigation record Markdown" value={reportText} readOnly />
        <label className="report-image-option"><input type="checkbox" checked={includeImages} onChange={e => setIncludeImages(e.target.checked)} disabled={exporting} /> Embed evidence frames in HTML (up to 24 frames, 16 MB)</label>
        <div className="report-actions">
          <button
            className="outline-button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(reportText);
                setReportStatus("Markdown copied.");
              } catch {
                setReportStatus("You can select text above and copy it using your browser.");
              }
            }}
          >
            Copy Markdown
          </button>
          <button className="outline-button" disabled={exporting} onClick={() => downloadReport("md")}>Download Markdown</button>
          <button className="outline-button" disabled={exporting} onClick={() => downloadReport("json")}>Download JSON</button>
          <button className="outline-button" disabled={exporting} onClick={() => downloadReport("html").catch(() => setReportStatus("The export was canceled or failed, please try again."))}>{exporting ? "Collect images…" : "Download HTML"}</button>
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
