/* Durable investigations, owned sources, and recoverable real-model requests. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const ui = () => window.FrameSeekSessionUI;
  const store = () => window.FrameSeekWorkbench;
  const demo = {id:"demo_basketball",name:"Basketball scenes · synthetic demo",metadata:{duration:18,audio:false},job:{state:"ready"}};
  const legacyDemo = {id:"demo_parcel",name:"Parcel movement · synthetic demo",metadata:{duration:18,audio:false},job:{state:"ready"}};
  const demos = new Map([[demo.id,demo],[legacyDemo.id,legacyDemo]]);
  const isDemo = id => demos.has(id);
  const labels = { queued:"Waiting for preflight", probing:"Checking video", captioning:"Analyzing frames", transcribing:"Transcribing audio", indexing:"Building index", ready:"Ready for questions", failed:"Processing failed", cancelled:"Cancelled", awaiting_configuration:"Waiting for model configuration" };
  const sourceReady = video => video.knowledge_status?.required_for_chat ? video.knowledge_status.state === 'ready' : video.job?.state === 'ready';
  function shortOcrRange(question, duration) {
    const scopeText=question.replace(/“[^”\n]*”|"[^"\n]*"|‘[^’\n]*’/g, match=>' '.repeat(match.length));
    const stamps=[...scopeText.matchAll(/(?<![\d:])\d{1,3}:[0-5]\d(?![\d:])/g)];
    const seconds=stamp=>{const [minutes,second]=stamp.split(':').map(Number);return minutes*60+second;};
    if(stamps.length===1) {
      const point=scopeText.match(/(?:\bat\s+|(?:请)?在\s*)(\d{1,3}:[0-5]\d)(?![\d:])(?!\s*(?:前|后|之前|之后|以前|以后|左右|附近|至|到|\b(?:or\s+)?(?:before|after|earlier|later)\b))/i);
      if(!point || /(?:不|未|没|\bnot\s+)\s*$/i.test(scopeText.slice(0,point.index)))return false;
      return Number.isFinite(duration) && seconds(point[1])<duration;
    }
    if(stamps.length!==2)return false;
    const range=scopeText.match(/(?:(\bbetween\b|\bfrom\b|请在|在)\s*)?(?<![\d:])(\d{1,3}:[0-5]\d)\s*(至|到|[-–—~～]|\bto\b|\band\b)\s*(\d{1,3}:[0-5]\d)(?![\d:])/i);
    if(!range || (range[3].toLowerCase()==='and' && range[1]?.toLowerCase()!=='between'))return false;
    const start=seconds(range[2]),end=seconds(range[4]);
    return Number.isFinite(duration) && start<end && end<=duration && end-start<=8;
  }
  function canSearchOcr(question) {
    return typeof question==='string' && !!currentVideo?.knowledge_status?.ocr_candidates_available
      && shortOcrRange(question,currentVideo.metadata?.duration)
      && /(?:文字|文本|text)\s*(?:包含|是|为|contains|[:：])?\s*[“"‘]/i.test(question);
  }
  function sourceStatus(video) {
    const knowledge=video.knowledge_status;
    if (!knowledge?.required_for_chat) return labels[video.job?.state] || 'Uploaded';
    const states={waiting_probe:'Waiting for video preflight',waiting_processor:'Waiting for index configuration',waiting_budget:'Waiting for budget or approval',queued:'Waiting to build index',indexing:'Building index',ready:'Ready for questions',cancelled:'Indexing stopped',failed:'Indexing failed',unknown:'Index status unknown',not_started:'Waiting to build index'};
    const status=states[knowledge.state] || 'Index not ready';
    return knowledge.state==='indexing' && knowledge.total_segments ? `${status} · ${knowledge.completed_segments}/${knowledge.total_segments} segments` : status;
  }
  const needsWatch = video => ["queued","probing","captioning","transcribing","indexing"].includes(video.job?.state) || (video.knowledge_status?.required_for_chat && ["waiting_probe","queued","indexing"].includes(video.knowledge_status.state));
  let session, current, currentVideo = demo, suspended = true, saving = null, lastSaved = "", lastSavedEdits = "", timer;
  let conflict = false, requestEpoch = 0, activeRequest = null, libraryEpoch = 0, submissionEpoch = 0, watchEpoch = 0, cancelEpoch = 0;
  let pendingSave = null;
  let lastVideoDesks = {};
  try {
    const saved = JSON.parse(localStorage.getItem("frameseek-video-desks") || "{}");
    if (saved && typeof saved === "object" && !Array.isArray(saved)) lastVideoDesks = saved;
  } catch {}
  const owns = (id, epoch) => current?.id === id && requestEpoch === epoch;
  let sourceChoices = new Map([[demo.id, demo]]), sourceListEpoch = 0, sourceSwitching = false;
  let searchEpoch = 0, searchDesk = null, searchGeneration = null;
  function syncSourceSwitcher(videos) {
    const localReady=!!session?.local_vision_ready;
    const cloudOption=$('cloud-engine-option');
    if(cloudOption){cloudOption.hidden=!session?.cloud_vision_ready;cloudOption.disabled=!session?.cloud_vision_ready;}
    $('local-engine-option').hidden=!localReady;
    $('local-engine-option').disabled=!localReady;

    $('auto-engine-indicator').textContent=$('chat-engine').value==='auto' ? 'Auto' : 'Manual';
    $('chat-engine').parentElement && ($('chat-engine').parentElement.hidden=isDemo(currentVideo.id));
    $('search-index').hidden = isDemo(currentVideo.id) || !sourceReady(currentVideo);
    if(!isDemo(currentVideo.id) && sourceReady(currentVideo)) $('composer-mode-text').textContent=currentVideo.knowledge_status?.required_for_chat ? (currentVideo.knowledge_status.processor_available===false ? "There are already indexes available for query · Local supplementary query is temporarily unavailable" : currentVideo.knowledge_status.processor==='synthetic_fixture' ? "Synthetic test index · Simulated supplementary check" : currentVideo.knowledge_status.processor==='tokenfactory' ? "Basic index · Cloud supplementary check requires budget access" : "Native index · On-demand re-checking") : $('chat-engine').value==='auto' ? 'Agent chooses a route · cloud processing requires budget approval' : $('chat-engine').value==='local' ? 'Local vision · free' : $('chat-engine').value==='index' ? 'Saved index · no model calls' : $('chat-engine').value==='cloud' ? "Cloud Vision · Budget access required" : "Model analysis · Budget approval required";
    if(searchDesk && (searchDesk !== current?.id || searchGeneration !== currentVideo.generation)) {
      searchEpoch++; searchDesk=null; $('visual-search-dialog').close(); $('visual-search-results').replaceChildren();
    }
    if (videos) sourceChoices = new Map([[demo.id, demo], ...videos.map(video => [video.id, video])]);
    sourceChoices.set(currentVideo.id, currentVideo);
    const select = $("source-switcher");
    select.replaceChildren();
    for (const video of sourceChoices.values()) {
      const option = document.createElement("option");
      option.value = video.id; option.textContent = video.name;
      option.dataset.thumbnail = isDemo(video.id) ? `/media/${video.id}/frames/0.jpg` : video.metadata?.preview?.[0]?.uri || "";
      const duration = Math.round(video.metadata?.duration || 0);
      option.dataset.detail = `${Math.floor(duration / 60).toString().padStart(2,"0")}:${(duration % 60).toString().padStart(2,"0")} · ${isDemo(video.id) ? "Synthetic demo" : sourceStatus(video)}`;
      select.append(option);
    }
    select.value = currentVideo.id;
    select.disabled = sourceSwitching;
    if (select.dispatchEvent) select.dispatchEvent(new Event("frameseek-sources"));
  }
  async function refreshSourceSwitcher() {
    const deskId = current?.id, epoch = requestEpoch, token = ++sourceListEpoch;
    const data = await api("/api/videos");
    if (owns(deskId, epoch) && token === sourceListEpoch) syncSourceSwitcher(data.items);
  }
  async function switchSelectedSource() {
    const video = sourceChoices.get($("source-switcher").value);
    if (!video || video.id === currentVideo.id || sourceSwitching) { syncSourceSwitcher(); return; }
    sourceSwitching = true; syncSourceSwitcher();
    try { await openSource(video); }
    catch (error) { ui().notify(`Failed to switch video:${error.message}. Your current investigation and draft were kept.`); }
    finally { sourceSwitching = false; syncSourceSwitcher(); }
  }

  function status(text, kind="") { $("save-state").textContent = kind === "saved" ? "Saved ✓" : text; $("save-state").title = text; $("save-state").dataset.state = kind; }
  async function api(path, method="GET", body) {
    const response = await fetch(path, {method, credentials:"same-origin", cache:"no-store",
      headers: body ? {"Content-Type":"application/json", "X-FS-CSRF":session.csrf} : method !== "GET" ? {"X-FS-CSRF":session.csrf} : {},
      body: body ? JSON.stringify(body) : undefined });
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) { const error = new Error(data?.detail || "request_failed"); error.status=response.status; error.data=data; throw error; }
    return data;
  }
  const snapshot = () => ui().snapshot();
  const wireSnapshot = state => {
    if(!session?.query_reference_snapshots || isDemo(currentVideo.id))return state;
    const reference = record => ({...record,query:{id:record.query.id,
      ...(record.query.parentTurn===undefined ? {} : {parentTurn:record.query.parentTurn})}});
    return {...state,records:(state.records || []).map(reference),pins:(state.pins || []).map(reference)};
  };
  function editSnapshot(state) {
    const {playerTime, ...panelState}=state.ui || {};
    return JSON.stringify({...state,ui:panelState});
  }
  function remember() {
    lastVideoDesks[currentVideo.id] = current.id;
    try {
      localStorage.setItem("frameseek-current", current.id);
      localStorage.setItem("frameseek-video-desks", JSON.stringify(lastVideoDesks));
    } catch {}
  }
  const draftShadowKey='frameseek-draft-shadow';
  function keepDraftShadow(base) {
    if(suspended || !current || !session?.owner_id)return;
    try {
      if(base===undefined)base=lastSaved ? JSON.parse(lastSaved).ui?.draft || '' : '';
      sessionStorage.setItem(draftShadowKey,JSON.stringify({owner:session.owner_id,desk:current.id,
        generation:currentVideo.generation || 0,epoch:store().getSnapshot().epoch || 0,
        base,text:$('question').value,expires:Date.now()+3600000,
        pendingDraft:pendingSave?.desk===current.id ? pendingSave.draft : null,
        pendingRevision:pendingSave?.desk===current.id ? pendingSave.revision+1 : null}));
    }catch{}
  }
  function recoverDraftShadow() {
    try {
      const saved=JSON.parse(sessionStorage.getItem(draftShadowKey) || 'null');
      if(!saved || saved.expires<Date.now() || typeof saved.text!=='string' || saved.text.length>4000
        || saved.owner!==session.owner_id || saved.desk!==current.id
        || saved.generation!==(currentVideo.generation || 0)
        || saved.epoch!==(store().getSnapshot().epoch || 0))return false;
      const pendingBase=current.revision===saved.pendingRevision && $('question').value===saved.pendingDraft;
      if($('question').value!==saved.base && $('question').value!==saved.text && !pendingBase)return false;
      if($('question').value===saved.text)return false;
      ui().restoreDraft(saved.text);return true;
    }catch{return false;}
  }
  async function flush() {
    if (saving) {
      const desk=current;
      await saving;
      if (current!==desk) return;
      if (editSnapshot(snapshot())!==lastSavedEdits) return flush();
      return;
    }
    if (suspended || conflict || !current) return;
    const state=snapshot(), encoded=JSON.stringify(state), edits=editSnapshot(state);
    if (encoded === lastSaved) return;
    const desk = current;
    status("Saving…");
    pendingSave={desk:desk.id,revision:desk.revision,draft:state.ui?.draft || ''};
    keepDraftShadow();
    saving = api(`/api/investigations/${desk.id}`, "PUT", {revision:desk.revision,state:wireSnapshot(JSON.parse(encoded))})
      .then(result => { desk.revision=result.revision; lastSaved=encoded; lastSavedEdits=edits; status("Saved","saved");
        if(current===desk) {
          if($('question').value===(state.ui?.draft || '')) {try{sessionStorage.removeItem(draftShadowKey);}catch{}}
          else keepDraftShadow(state.ui?.draft || '');
        }
      })
      .catch(error => { conflict=error.status===409; status(conflict ? "Other pages have been modified · Please reopen" : "Save failed · Click to try again", "error"); throw error; })
      .finally(() => { saving=null; pendingSave=null; });
    await saving;
    if (current!==desk) return;
    if (editSnapshot(snapshot())!==lastSavedEdits) return flush();
  }
  function changed() {
    if (suspended || conflict) return;
    status("There are unsaved changes"); clearTimeout(timer);
    timer=setTimeout(() => flush().catch(() => {}),500);
  }
  async function openDesk(id) {
    const previousId=current?.id, epoch=++requestEpoch;
    ui().busy(true);
    try {
      await flush(); if (!owns(previousId,epoch)) return false;
      await stopRequest(); if (!owns(previousId,epoch)) return false;
      const desk=await api(`/api/investigations/${id}`);
      if (!owns(previousId,epoch)) return false;
      const video=demos.get(desk.video) || await api(`/api/videos/${desk.video}`);
      if (!owns(previousId,epoch)) return false;
      if (isDemo(video.id)) await ui().prepareDemo?.(video.id);
      if (!owns(previousId,epoch)) return false;
      await flush(); if (!owns(previousId,epoch)) return false;
      if (desk.id===previousId && !conflict && current.revision>desk.revision) {desk.state=snapshot();desk.revision=current.revision;}
      suspended=true;
      current=desk; currentVideo=video; conflict=false;
      ui().restore({...desk.state,title:desk.state.title || desk.title},video);
      const restored=snapshot();lastSaved=JSON.stringify(restored);lastSavedEdits=editSnapshot(restored);
      remember(); syncSourceSwitcher(); status("Investigation restored","saved");
      $("workspace-library").close();
      suspended=false;
      if(recoverDraftShadow())changed();
      await recover();
      if (!owns(desk.id,epoch)) return false;
      if (needsWatch(video)) watch(video.id,epoch);
      refreshChatBudget();
      return true;
    } finally { if (epoch===requestEpoch) {suspended=false;ui().busy(false);} }
  }
  async function create(video=demo) {
    const previousId=current?.id, epoch=++requestEpoch;
    ui().busy(true);
    try {
      await flush(); if (!owns(previousId,epoch)) return false;
      await stopRequest(); if (!owns(previousId,epoch)) return false;
      if (isDemo(video.id)) await ui().prepareDemo?.(video.id);
      if (!owns(previousId,epoch)) return false;
      const desk=await api("/api/investigations","POST",{video_id:video.id,title:video.id === demo.id ? "Basketball demo" : video.name.replace(/\.[^.]+$/, "")});
      if (!owns(previousId,epoch)) return false;
      await flush(); if (!owns(previousId,epoch)) return false;
      suspended=true;
      current=desk;
      currentVideo=video; conflict=false;
      ui().applySource(video); store().rename(current.title);
      lastSaved=""; lastSavedEdits="";remember(); syncSourceSwitcher();
      suspended=false;
      await flush(); if (!owns(desk.id,epoch)) return false;
      $("workspace-library").close();
      if (needsWatch(video)) watch(video.id,epoch);
      refreshChatBudget();
      return true;
    } finally { if (epoch===requestEpoch) {suspended=false;ui().busy(false);} }
  }
  function item(title, detail, action, actionLabel="Open") {
    const row=document.createElement("article"); row.className="library-row";
    const copy=document.createElement("div"); const name=document.createElement("strong"); name.textContent=title;
    const meta=document.createElement("small");meta.textContent=detail;copy.append(name,meta);row.append(copy);
    const button=document.createElement("button");button.className="outline-button";button.textContent=actionLabel;
    button.onclick=()=>Promise.resolve(action()).catch(error=>libraryNotice(error.message));row.append(button);return row;
  }
  function libraryNotice(message) { $("library-notice").textContent=message; }
  let budgetEpoch=0,preflightNotice=false;
  async function refreshChatBudget() {
    const line=$('chat-budget-status');
    if(!line)return;
    const desk=current?.id,epoch=requestEpoch,check=++budgetEpoch;
    $('chat-engine').disabled=!!currentVideo.knowledge_status?.required_for_chat;
    if(currentVideo.knowledge_status?.required_for_chat) {line.hidden=false;line.textContent=currentVideo.knowledge_status.processor_available===false?"There is an existing index that can be queried; local retrieval is temporarily unavailable.":"Automatically read the basic index; only check relevant screens when needed.";return;}
    if(isDemo(currentVideo.id)) {line.hidden=true;return;}
    if($('chat-engine').value==='auto') {line.hidden=false;line.textContent="Check existing evidence first, and select available visual analysis if necessary; cloud processing is still limited by the original budget, and failure will not automatically switch to retry.";return;}
    if($('chat-engine').value==='local') {
      line.hidden=false;line.textContent="Native visual recognition, without using Token Factory quota. Scan the entire film for the first time, and the completed fragments will be cached; the paid API will not be automatically called.";return;
    }
    try {
      const data=await api('/api/runtime/status');
      if(check!==budgetEpoch || !owns(desk,epoch))return;
      const b=data.budget;
      line.hidden=false;
      const prefix=$('chat-engine').value==='index' ? 'Index questions use existing data without model calls.' : '';
      line.textContent=prefix+(b && b.remaining_calls===0
        ? `Model requests exhausted ${b.call_cap} / ${b.call_cap} times; the model will not be called again, and existing answers and evidence can still be reviewed.`
        : !data.dispatch_prerequisites_met
        ? "Model analysis is not ready. Check More → Models & budget. Existing evidence is still available."
        : b?.visual_search_calls === 1 ? `${b.remaining_calls} / ${b.call_cap} model requests remaining · Visual search usually reserves one request and reviews up to 8 indexed candidates; it does not exhaustively check the video.`
        : b ? `${b.remaining_calls} / ${b.call_cap} model requests remaining · A period of up to 60 seconds usually reserves ${b.short_window_calls} requests; searching across periods requires more.`
        : "Budget status is unconfirmed. Check Models & budget before sending.");
    }catch(error){if(check===budgetEpoch && owns(desk,epoch)){line.hidden=false;line.textContent="The budget status reading failed and the ledger check still needs to be passed before sending; the model will not be automatically called.";}}
  }
  async function showRuntime() {
    const dialog=$("agent-setup-dialog"), button=$("refresh-agent-setup");
    if(!dialog.open) dialog.showModal();
    button.disabled=true;$("agent-setup-summary").textContent="Reading local configuration...";
    try {
      const data=await api("/api/runtime/status");
      const provider=data.provider==="gemini" ? "Gemini · AI Builder Cup" : data.provider==="tokenfactory" ? "NVIDIA Nemotron · Token Factory" : "NVIDIA / Nebius";
      $("agent-setup-summary").textContent=data.dispatch_prerequisites_met
        ? `${provider}: Chat access conditions are complete, and videos that have been indexed can be asked directly without re-analysis. The check only reads the configuration and ledger and cannot prove that every answer is accurate.`
        : `${provider}: Real analysis is not yet ready. The demo, upload preflight, playback, and saved investigations are still available.`;
      const list=$("agent-readiness-checks");list.replaceChildren();
      for(const check of data.checks) {
        const row=document.createElement("article");row.className="agent-check";
        const heading=document.createElement("strong");heading.textContent=check.label;
        const state=document.createElement("span");state.textContent=check.state==="available" ? "Provided" : "Not ready";state.dataset.state=check.state;
        const detail=document.createElement("p");detail.textContent=check.detail;
        row.append(heading,state,detail);list.append(row);
      }
      const models=$("agent-model-roles");models.replaceChildren();
      for(const model of data.models) {
        const row=document.createElement("p");row.textContent=`${model.label}${model.optional ? " (optional)" : ""} · ${model.configured ? `${model.model} / ${model.revision} · ${model.key_present ? "Credentials configured" : "Credentials missing"}` : "Not configured"}`;
        models.append(row);
      }
    }catch(error){$("agent-setup-summary").textContent=`Read failed:${error.message}. Can be rechecked.`;}
    finally {button.disabled=false;}
  }
  async function searchSavedFrames(savedQuestion=null) {
    const question=typeof savedQuestion==='string' ? savedQuestion.trim() : typeof savedQuestion?.question==='string' ? savedQuestion.question.trim() : $('question').value.trim();
    const answerRequestId=typeof savedQuestion?.answerRequestId==='string' ? savedQuestion.answerRequestId : null;
    let autoOcr=!!savedQuestion?.autoOcr;
    if(!question) {ui().notify("First describe what you are looking for in the input box, and then search for existing images; it will not be sent to the model.");return;}
    const desk=current?.id, epoch=requestEpoch, source=currentVideo, generation=source.generation, check=++searchEpoch;
    if(!desk || isDemo(source.id) || !sourceReady(source))return;
    const dialog=$('visual-search-dialog'), list=$('visual-search-results');
    searchDesk=desk; searchGeneration=generation; list.replaceChildren();
    $('visual-search-summary').textContent="Retrieving existing index, not calling model...";
    if(!dialog.open)dialog.showModal();
    let offset=0, snapshot=null, displayed=0, selectedConditions=null, filterRevision=0, candidateSection=null;
    const active=()=>check===searchEpoch && owns(desk,epoch) && currentVideo.id===source.id && currentVideo.generation===generation && dialog.open;
    $('visual-search-conditions').textContent='';$('visual-search-coverage').textContent='';
    $('visual-search-track').replaceChildren();$('visual-search-track').hidden=true;
    $('visual-search-options').replaceChildren();$('visual-search-filters').hidden=true;
    $('visual-search-apply').onclick=()=>{
      if(!active())return;
      selectedConditions=[...$('visual-search-options').children].filter(row=>row.firstElementChild.checked).map(row=>JSON.parse(row.dataset.condition));
      if(!selectedConditions.length){ui().notify("Please select at least one explicit condition.");return;}
      filterRevision++;offset=0;snapshot=null;displayed=0;candidateSection=null;list.replaceChildren();$('visual-search-track').replaceChildren();loadPage();
    };
    async function loadPage(button=null) {
      const revision=filterRevision, currentResult=()=>active() && revision===filterRevision;
      if(!currentResult())return;
      if(button)button.disabled=true;
      try {
        const data=await api(`/api/videos/${source.id}/occurrences/search`,'POST',
          {question,generation,offset,limit:40,snapshot,conditions:selectedConditions,answer_request_id:answerRequestId});
        if(!currentResult())return;
        const options=$('visual-search-options');
        if(!offset) {
          options.replaceChildren();
          for(const condition of data.available_conditions || []) {
            const row=document.createElement('label'), input=document.createElement('input'), text=document.createElement('span');
            input.type='checkbox';input.checked=!!selectedConditions?.some(c=>c.field===condition.field && c.value===condition.value);
            text.textContent=`${condition.field}：${condition.value}`;row.dataset.condition=JSON.stringify(condition);row.append(input,text);options.append(row);
          }
          $('visual-search-filters').hidden=!options.children.length;
        }
        if(data.kind==='clarification_required') {
          $('visual-search-summary').textContent=data.message;
          $('visual-search-coverage').textContent=autoOcr
            ? "The text candidate is not running: please follow the prompts to clarify the search conditions first; the existing index has not been changed and the model has not been called."
            : "No new visual recognition is performed; existing indexes can be retrieved after clarifying the conditions.";return;
        }
        if(button)button.remove();
        const structured=data.kind==='indexed_occurrences', rows=structured ? data.occurrences : data.frames;
        displayed+=rows.length;
        $('visual-search-conditions').textContent=data.conditions?.length ? `${data.explicit_conditions ? "Selected conditions (replace the original question)" : "All conditions"}：${data.conditions.map(c=>`${c.field} ${c.operator==='is_not' ? 'is not' : c.operator==='contains' || c.operator==='color_item' ? 'contains' : 'is'} ${c.value}`).join('；')}(same object)` : `Search concepts:${(data.terms || []).join(' + ')}`;
        const coverage=data.coverage || {};
        $('visual-search-summary').textContent=structured
          ? `Existing observations match ${displayed} / ${data.total_occurrences} ${coverage.catalog_complete === false ? "sampled frame times (not a count of people or events)" : "intervals or times"}. No model calls.`
          : `Searched descriptions of all ${data.indexed_frames} saved frames; showing ${displayed} / ${data.total_candidates ?? rows.length} text candidates. Candidates are not confirmed matches and do not represent every appearance in the video. No model calls.`;
        $('visual-search-coverage').textContent=structured
          ? `${data.provenance} · Reviewed observations for ${coverage.reviewed_frames} / ${coverage.catalog_frames} indexed frames; ${coverage.missing_frame_ids?.length || 0} missing and ${coverage.uncertain_frame_ids?.length || 0} uncertain. ${coverage.catalog_complete === false ? "The index contains sampled frames, not every video frame. " : ""}${coverage.exhaustive_under_index ? "All source frames in the index were searched; accuracy depends on the original observations." : "Coverage is incomplete; absence and all appearance times cannot be established."}`
          : "Full film structured observation index missing. The following only searches for existing descriptions, and cannot confirm multiple attributes of the same subject, nor can it infer continuous occurrence intervals.";
        if(autoOcr && (data.ocr_eligible!==true || !canSearchOcr(question))) {
          autoOcr=false;
          $('visual-search-coverage').textContent+=!currentVideo?.knowledge_status?.ocr_candidates_available
            ? " Text candidate not running: OCR is not enabled for the current service. Model not called."
            : " Text candidates are unavailable: currently only English and numbers are supported; there may be no sampling frames available during that time period, or the question may contain unsupported conditions. Model not called.";
        }
        if(!rows.length) {const empty=document.createElement('p');empty.textContent=coverage.exhaustive_under_index ? "There are currently no matches in existing observations." : "No match found for current index; unchecked ranges may still occur.";list.append(empty);}
        if(!offset && !selectedConditions && data.ocr_eligible===true && canSearchOcr(question)) {
          const ocr=document.createElement('button');ocr.type='button';ocr.textContent="Find text candidates for this period · OCR";
          ocr.onclick=async()=>{
            if(!currentResult())return;
            ocr.disabled=true;$('visual-search-summary').textContent="Looking for text candidates…";
            try {
              const candidates=await api(`/api/videos/${source.id}/ocr-candidates`,'POST',{question,generation});
              if(!currentResult())return;
              if(!data.knowledge_version || candidates.knowledge_version!==data.knowledge_version) {
                ocr.remove();
                $('visual-search-summary').textContent="Index version has changed or cannot be confirmed; old answers are not mixed with new text candidates. Please re-query the latest index from the current video.";
                const latest=document.createElement('button');latest.type='button';latest.textContent="View the latest index";
                latest.onclick=()=>{if(currentResult())searchSavedFrames({question});};
                list.append(latest);
                return;
              }
              ocr.remove();
              const observed=new Set(structured ? rows.flatMap(entry=>(entry.evidence || []).map(frame=>frame.frame_id).filter(Boolean)) : rows.map(frame=>frame.frame_id).filter(Boolean));
              const newCandidates=candidates.frames.filter(frame=>!observed.has(frame.frame_id));
              $('visual-search-summary').textContent=`Existing index results are retained; OCR found ${newCandidates.length} additional unverified candidate frames. Review them in the source video.`;
              $('visual-search-coverage').textContent+=` OCR checked ${candidates.checked_frames} sampled frames; ${candidates.failed_frames} failed. This cannot establish absence at other times. No cloud model was called.`;
              for(const frame of newCandidates) {
                if(!frame.uri.startsWith(`/media/${source.id}/frames/`))continue;
                const row=document.createElement('article'), image=document.createElement('img'), caption=document.createElement('p'), seek=document.createElement('button');
                image.src=frame.uri;image.alt="OCR text candidate, need to review and verify";image.loading='lazy';
                caption.textContent="OCR candidate · To be verified, the text may be recognized incorrectly";
                seek.type='button';seek.textContent=`Replay ${Math.floor(frame.t/60).toString().padStart(2,'0')}:${(frame.t%60).toFixed(3).padStart(6,'0')} · To be verified`;
                seek.onclick=()=>{if(!currentResult())return;dialog.close();ui().seek(frame.t,seek);};
                row.append(image,caption,seek);list.append(row);
              }
            } catch(error) {if(currentResult())$('visual-search-summary').textContent="The text candidate is not completed. Please specify a time period of no longer than 8 seconds and text in quotes, or try again later; existing indexes have not changed.";}
            finally {ocr.disabled=false;}
          };list.append(ocr);
          if(autoOcr){autoOcr=false;await ocr.onclick();if(!currentResult())return;}
        }
        for(const entry of rows) {
          const frame=structured ? entry.evidence[0] : entry;
          if(!frame?.uri.startsWith(`/media/${source.id}/frames/`))continue;
          const row=document.createElement('article'), image=document.createElement('img');
          const caption=document.createElement('p'), seek=document.createElement('button');
          image.src=frame.uri; image.alt=structured ? "Original video evidence of structured observation is available" : "Saved candidate frames of the original video";image.loading='lazy';
          caption.textContent=structured ? `The subject of the same picture meets all conditions · ${entry.evidence_count} Frame support; does not represent the same person across shots.` : frame.caption;
          const clock=t=>`${Math.floor(t/60).toString().padStart(2,'0')}:${(t%60).toFixed(3).padStart(6,'0')}`;
          seek.textContent=structured ? `Replay ${clock(entry.start)}${entry.end==null ? ' · Sampled time' : `—${clock(entry.end)}`} · Observed` : `Replay ${clock(frame.t)} · Index candidates`;
          seek.type='button';seek.onclick=()=>{if(!currentResult())return;dialog.close();ui().seek(frame.t,seek);};
          if(structured && data.duration>0) {
            const marker=document.createElement('button');marker.type='button';
            marker.textContent='';marker.title=seek.textContent;marker.setAttribute('aria-label',`Timeline ${clock(entry.start)}`);
            marker.style.left=`${100*entry.start/data.duration}%`;
            marker.style.width=`${Math.max(.5,100*((entry.end ?? entry.start+.001)-entry.start)/data.duration)}%`;
            marker.onclick=seek.onclick;$('visual-search-track').append(marker);$('visual-search-track').hidden=false;
          }
          row.append(image,caption,seek);candidateSection ? list.insertBefore(row,candidateSection) : list.append(row);
        }
        if(!offset && structured && Array.isArray(data.uncertain_candidates) && data.uncertain_candidates.length) {
          candidateSection=document.createElement('section');candidateSection.className='uncertain-candidate-group';
          candidateSection.setAttribute('aria-label',"Unverified candidate frames");
          const heading=document.createElement('p');
          heading.textContent=`Unverified candidates · ${data.uncertain_candidate_total} total; some conditions are unclear, so these are excluded from matches.`;
          const controls=document.createElement('div'), timeInput=document.createElement('input'), jump=document.createElement('button');
          controls.className='candidate-time-controls';timeInput.type='text';timeInput.placeholder="For example 05:20";
          timeInput.setAttribute('aria-label',"Candidate picture start time");
          jump.type='button';jump.textContent="jump to time";controls.append(timeInput,jump);
          candidateSection.append(heading,controls);list.append(candidateSection);
          const candidateSnapshot=data.snapshot, candidateVersion=data.knowledge_version;
          let candidateEpoch=0;
          const appendCandidates=page=>{
            for(const frame of page.uncertain_candidates || []) {
              if(typeof frame.uri!=='string' || !frame.uri.startsWith(`/media/${source.id}/frames/`) || !Number.isFinite(frame.t) || frame.t<0 || frame.t>=data.duration)continue;
              const row=document.createElement('article'), image=document.createElement('img'), caption=document.createElement('p'), seek=document.createElement('button');
              image.src=frame.uri;image.alt="Candidate pictures with unclear conditions";image.loading='lazy';
              caption.textContent="Unverified candidate · some conditions are unclear; review the frame";
              seek.type='button';seek.textContent=`Replay ${Math.floor(frame.t/60).toString().padStart(2,'0')}:${(frame.t%60).toFixed(3).padStart(6,'0')} · To be verified`;
              seek.onclick=()=>{if(!currentResult())return;dialog.close();ui().seek(frame.t,seek);};
              row.append(image,caption,seek);candidateSection.append(row);
            }
            if(page.uncertain_candidate_next_offset!=null) {
              const moreCandidates=document.createElement('button');moreCandidates.type='button';
              moreCandidates.textContent="Load more unverified frames · No model calls";
              moreCandidates.onclick=async()=>{
                if(!currentResult())return;const request=++candidateEpoch;moreCandidates.disabled=true;
                try {
                  const next=await api(`/api/videos/${source.id}/occurrences/search`,'POST',{
                    question,generation,offset:0,limit:40,snapshot:candidateSnapshot,conditions:selectedConditions,
                    answer_request_id:answerRequestId,candidate_offset:page.uncertain_candidate_next_offset
                  });
                  if(!currentResult() || request!==candidateEpoch)return;
                  if(next.snapshot!==candidateSnapshot || next.knowledge_version!==candidateVersion)throw new Error('index_version_changed');
                  moreCandidates.remove();appendCandidates(next);
                } catch(error) {if(currentResult() && request===candidateEpoch)$('visual-search-coverage').textContent+=" Failed to load candidate frames. Reopen the index; no model was called.";}
                finally {moreCandidates.disabled=false;}
              };candidateSection.append(moreCandidates);
            }
          };
          jump.onclick=async()=>{
            if(!currentResult())return;
            const match=timeInput.value.trim().match(/^(\d{1,3}):([0-5]\d)$/);
            const seconds=match ? Number(match[1])*60+Number(match[2]) : NaN;
            if(!Number.isFinite(seconds) || seconds>=data.duration) {heading.textContent="Please enter a time within the video range, for example 05:20.";return;}
            const request=++candidateEpoch;jump.disabled=true;
            try {
              const page=await api(`/api/videos/${source.id}/occurrences/search`,'POST',{
                question,generation,offset:0,limit:40,snapshot:candidateSnapshot,conditions:selectedConditions,
                answer_request_id:answerRequestId,candidate_time:seconds
              });
              if(!currentResult() || request!==candidateEpoch)return;
              if(page.snapshot!==candidateSnapshot || page.knowledge_version!==candidateVersion)throw new Error('index_version_changed');
              candidateSection.replaceChildren(heading,controls);
              heading.textContent=`Unverified candidates · ${data.uncertain_candidate_total} total; showing frames from ${timeInput.value.trim()}. These are excluded from matches.`;
              if(!page.uncertain_candidates?.length) {const empty=document.createElement('p');empty.textContent="There are no candidates to be verified after this time.";candidateSection.append(empty);}
              appendCandidates(page);
            } catch(error) {if(currentResult() && request===candidateEpoch)heading.textContent="Jump failed, please reopen the index; the model was not called.";}
            finally {jump.disabled=false;}
          };
          appendCandidates(data);
        }
        if(data.next_offset!=null) {
          offset=data.next_offset;snapshot=data.snapshot;
          const more=document.createElement('button');more.type='button';
          more.textContent="Load later times · No model calls";more.onclick=()=>currentResult() && loadPage(more);
          candidateSection ? list.insertBefore(more,candidateSection) : list.append(more);
        }
      } catch(error) {
        if(currentResult() && error.message==='index_answer_version_unavailable') {
          $('visual-search-summary').textContent="This old answer does not have an indexed version saved, so the original basis cannot be confirmed. The latest index can be viewed; original answers and chats will not change.";
          const latest=document.createElement('button');latest.type='button';latest.textContent="View the latest index";
          latest.onclick=()=>{if(currentResult())searchSavedFrames({question});};list.append(latest);
        } else if(currentResult())$('visual-search-summary').textContent=`Index is temporarily unavailable:${error.message}. The original question and chat are retained, and the model is not called.`;
      }
      finally {if(button)button.disabled=false;}
    }
    await loadPage();
  }

  async function library(kind="investigations") {
    const deskId=current?.id, epoch=requestEpoch, token=++libraryEpoch;
    const active=()=>owns(deskId,epoch) && token===libraryEpoch;
    await flush(); if (!active()) return; libraryNotice("");
    $("library-title").textContent=kind === "videos" ? "Material library" : "Investigations";
    $("library-intro").textContent=kind === "videos" ? "Each video keeps its own questions and evidence. Uploads are checked before analysis under the approved budget." : `Investigations are saved on this server for ${session.ttl_hours} hours by default. Reopen an investigation or create a new one.`;
    $("library-add").textContent=kind === "videos" ? "Upload video" : "New investigation";
    $("library-add").onclick=()=>{
      if(kind === "videos") {$("workspace-library").close();$("file-input").click();}
      else create(currentVideo).catch(e=>libraryNotice(e.message));
    };
    const body=$("library-items");body.replaceChildren();
    $("workspace-library").showModal();
    if (kind === "videos") {
      body.append(item(demo.name,"18 seconds · Synthetic demo · Preset answers",()=>openSource(demo)));
      const data=await api("/api/videos");
      if (!active()) return;
      if (data.items.some(video=>video.knowledge_status?.required_for_chat)) $("library-intro").textContent="Each video keeps its own questions and evidence. After upload, preflight and indexing must finish before questions are available.";
      syncSourceSwitcher(data.items);
      for (const video of data.items) {
        const job=video.job;
        const row=item(video.name,`${Math.round(video.bytes/1024/1024)} MB · ${sourceStatus(video)}${!video.knowledge_status?.required_for_chat && job?.total ? ` · ${job.completed}/${job.total} windows` : ""}`,()=>openSource(video));
        if (["failed","cancelled"].includes(job?.state) && !video.metadata?.windows) {
          const retry=document.createElement("button");retry.className="outline-button";retry.textContent="Retry preflight";
          retry.onclick=async()=>{try {await api(`/api/videos/${video.id}/retry`,"POST");libraryNotice("Preflight restarted.");if(currentVideo.id===video.id) watch(video.id,requestEpoch);}catch(e){libraryNotice(e.message);}};
          row.append(retry);
        } else if (["awaiting_configuration","failed","cancelled"].includes(job?.state)) {
          const analyze=document.createElement("button");analyze.className="outline-button";analyze.textContent="Start analysis";
          analyze.disabled=!session.real_ready || !video.metadata?.windows;
          analyze.title=analyze.disabled ? "Requires model services, valid calibration records, and approved budget" : "Analyze full footage using approved budget";
          analyze.onclick=async()=>{try {await api(`/api/videos/${video.id}/analyze`,"POST"); libraryNotice("Analysis started. Refresh to check progress."); if (currentVideo.id===video.id) watch(video.id,requestEpoch);}catch(e){libraryNotice(e.message);}};
          row.append(analyze);
        }
        if (["queued","probing","captioning","transcribing","indexing"].includes(job?.state)) {
          const cancel=document.createElement("button");cancel.className="text-button";cancel.textContent="Cancel";
          cancel.onclick=async()=>{await api(`/api/videos/${video.id}/cancel`,"POST");await library("videos");};row.append(cancel);
        }
        const rename=document.createElement("button");rename.className="text-button";rename.textContent="Rename";
        rename.onclick=()=>renameSource(video);row.append(rename);
        const remove=document.createElement("button");remove.className="text-button danger";remove.textContent="Delete";
        remove.setAttribute("aria-label",`Delete video: ${video.name}`);
        remove.onclick=()=>confirmDelete(video,"videos");row.append(remove);body.append(row);
      }
      if (!session.real_ready && !data.items.some(video=>video.knowledge_status?.required_for_chat)) libraryNotice("Analysis requires model configuration and budget approval. Upload, preflight, playback and saved investigations are available.");
    } else {
      const data=await api("/api/investigations");
      if (!active()) return;
      for (const desk of data.items) {
        const row=item(desk.title,`${isDemo(desk.video) ? "Synthetic demo" : "Uploaded video"} · ${new Date(desk.updated*1000).toLocaleString()}${desk.id===current?.id ? " · Current" : ""}`,()=>openDesk(desk.id));
        const remove=document.createElement("button");remove.className="text-button danger";remove.textContent="Delete";
        remove.onclick=()=>confirmDelete(desk,"investigations");row.append(remove);body.append(row);
      }
    }
    $("library-refresh").onclick=()=>library(kind).catch(e=>libraryNotice(e.message));
  }
  function renameSource(video) {
    $("rename-source-input").value=video.name;
    $("rename-source-form").onsubmit=async event=>{
      event.preventDefault();
      try {
        const result=await api(`/api/videos/${video.id}`,"PATCH",{name:$("rename-source-input").value});
        if(currentVideo.id===video.id) {currentVideo={...currentVideo,name:result.name};ui().renameSource(result.name);}
        syncSourceSwitcher();
        $("rename-source-dialog").close();await library("videos");
      } catch(error) {libraryNotice(error.message);}
    };
    $("rename-source-cancel").onclick=()=>$("rename-source-dialog").close();
    $("rename-source-dialog").showModal();$("rename-source-input").focus();
  }
  function confirmDelete(entity,table) {
    $("delete-description").textContent=table === "videos" ? `Delete "${entity.name}" and all its investigations, evidence, and original video?` : `Delete investigation "${entity.title}"?`;
    $("delete-confirm").onclick=async()=>{
      const deskId=current?.id;
      const deletingCurrent=(table === "videos" && current?.video===entity.id) || deskId===entity.id;
      const epoch=deletingCurrent ? ++requestEpoch : requestEpoch;
      if (deletingCurrent) ui().busy(true);
      try {
        if (deletingCurrent) {
          await flush(); if (!owns(deskId,epoch)) return;
          await stopRequest(); if (!owns(deskId,epoch)) return;
          suspended=true;
        }
        await api(`/api/${table}/${entity.id}`,"DELETE");
        if (!owns(deskId,epoch)) return;
        $("delete-dialog").close();
        suspended=false;
        if (deletingCurrent) { current=null; if (!await create(demo)) return; }
        await library(table);
      } catch(e) {if (owns(deskId,epoch)) {$("delete-dialog").close();libraryNotice(e.message);}}
      finally {if (epoch===requestEpoch) {suspended=false;if (deletingCurrent) ui().busy(false);}}
    };
    $("delete-dialog").showModal();
  }
  async function openSource(video) {
    const deskId=current?.id, epoch=++requestEpoch;
    ui().busy(true);
    try {
      await flush(); if (!owns(deskId,epoch)) return false;
      const desks=(await api("/api/investigations")).items.filter(d=>d.video===video.id);
      if (!owns(deskId,epoch)) return false;
      if (desks.length) {
        const remembered = desks.find(desk => desk.id === lastVideoDesks[video.id]);
        return await openDesk((remembered || desks[0]).id);
      }
      return await create(video);
    } finally {if (owns(deskId,epoch)) ui().busy(false);}
  }
  function focusDemoQuestion() {
    for (const selector of ['.reading-demo-examples .suggestion-buttons button', '#suggestions button']) {
      const target=document.querySelector?.(selector);
      if (target && (!target.getClientRects || target.getClientRects().length)) {target.focus();return;}
    }
    $('question')?.focus();
  }
  async function upload(file) {
    if (!file.type.startsWith("video/") || file.size > 500*1024**2) {ui().notify("Please select a video file that is no larger than 500 MB.");return;}
    try {
      const deskId=current?.id, epoch=requestEpoch;
      await flush();if (!owns(deskId,epoch)) return;ui().notify(`Uploading ${file.name} · 0%`);
      const result=await new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();xhr.open("POST","/api/videos");
        xhr.setRequestHeader("Content-Type",file.type);xhr.setRequestHeader("X-FS-CSRF",session.csrf);
        xhr.setRequestHeader("X-File-Name",encodeURIComponent(file.name));xhr.timeout=180000;
        xhr.upload.onprogress=e=>{if(owns(deskId,epoch) && e.lengthComputable)ui().notify(`Uploading ${file.name} · ${Math.round(e.loaded/e.total*100)}%`);};
        xhr.onload=()=>{let data;try {data=JSON.parse(xhr.responseText);}catch{reject(new Error("Invalid upload response"));return;}xhr.status===201 ? resolve(data) : reject(new Error(data.detail || "Upload failed"));};
        xhr.onerror=()=>reject(new Error("Upload interrupted. The original investigation was kept."));xhr.ontimeout=xhr.onerror;xhr.send(file);
      });
      if (!owns(deskId,epoch)) return;
      const video=await api(`/api/videos/${result.id}`);if (!owns(deskId,epoch)) return;
      if (!await create(video)) return;
      ui().notify("Upload complete. The server is checking the video; view progress in Manage videos.");
      ui().showSource();
    }catch(error){ui().notify(error.message);}
  }
  async function watch(videoId, epoch) {
    const deskId=current?.id, token=++watchEpoch;
    const knowledgeWatch=!!currentVideo?.knowledge_status?.required_for_chat;
    const active=()=>token===watchEpoch && owns(deskId,epoch) && currentVideo?.id===videoId;
    let readFailures=0;
    for (let i=0;active() && (knowledgeWatch || i<90);i++) {
      await new Promise(r=>setTimeout(r,readFailures ? Math.min(16000,2000*2**readFailures) : 2000));
      if (!active()) return;
      let video;
      try {
        video=await api(`/api/videos/${videoId}`);
      } catch(error) {
        if (!active()) return;
        if (knowledgeWatch && (error.status==null || error.status===429 || error.status>=500) && ++readFailures<=3) {
          ui().notify("Temporarily unable to read indexing progress, reconnecting; video will not be reprocessed.");
          continue;
        }
        ui().notify(knowledgeWatch ? "Unable to read indexing progress. Please reopen the video to view; completed processing will be retained." : error.message);
        return;
      }
      if (!active()) return;
      readFailures=0;
      try {
        const knowledge=video.knowledge_status;
        currentVideo=video;ui().updateKnowledge?.(video);
        if (knowledge?.required_for_chat && ["queued","indexing"].includes(knowledge.state)) {
          const label=`Building index · ${knowledge.completed_segments}/${knowledge.total_segments} segments`;
          $("video-state").textContent=label;ui().notify(label);continue;
        }
        ui().notify(`${labels[video.job?.state] || "Processing video"}${video.job?.total ? ` · ${video.job.completed}/${video.job.total} windows` : ""}`);
        if (["ready","failed","awaiting_configuration","cancelled"].includes(video.job?.state) && !needsWatch(video)) {
          await flush(); if (!active()) return;
          currentVideo=video; const saved=snapshot();suspended=true;
          try {ui().restore(saved,video);} finally {suspended=false;}
          syncSourceSwitcher();
          refreshChatBudget();
          changed();
          if (knowledge?.required_for_chat && knowledge.state === "ready") ui().notify("The basic index is complete and you can start asking questions.");
          else if (knowledge?.required_for_chat && knowledge.state === "waiting_budget") ui().notify("Basic indexing awaits budget or authorization; videos and existing answers will still be viewable, and completed clips will be retained.");
          else if (knowledge?.required_for_chat && ["failed","unknown","cancelled","waiting_processor"].includes(knowledge.state)) ui().notify("Basic indexing is not yet complete; videos and existing answers are still viewable.");
          else if (video.job?.state === "failed") ui().notify(`Preflight or analysis failed:${video.job.error}. The video is retained and can be reprocessed or deleted.`);
          else if (video.job?.state === "awaiting_configuration") ui().notify("Preflight complete. Waiting for model service and budget configuration. No model request will be sent.");
          return;
        }
      }catch(error){if (active()) ui().notify(error.message);return;}
    }
  }
  async function recover({resumeMissing=false}={}) {
    if (!current || isDemo(currentVideo.id)) return;
    const deskId=current.id, epoch=requestEpoch, deskEpoch=store().getSnapshot().epoch,
      submission=submissionEpoch, initialDraft=$("question").value;
    const valid=()=>owns(deskId,epoch) && store().getSnapshot().epoch===deskEpoch && submission===submissionEpoch;
    $('resume-query').hidden=true;
    const data = await api(`/api/investigations/${deskId}/requests`);
    if (!valid()) return;
    const existing=new Set(store().getSnapshot().records.map(r=>r.query.id));
    const completed=data.items.filter(r=>r.state === "done" && r.result?.response?.schema_version && !isEmptyCancellation(r.result)).slice(0,100).reverse();
    let added=false;
    for (const request of completed) {
      if (!existing.has(request.id)) {
        const parent=store().getSnapshot().records.find(r=>r.query.id===request.input?.parent_request_id);
        ui().answer({...request.result,parentTurn:parent?.id || null});
        added=true;
      }
      if (activeRequest?.desk===deskId && activeRequest.id===request.id) activeRequest=null;
    }
    let draft=$("question").value;
    for (const request of data.items) {
      if (request.state === "done" && activeRequest?.desk===deskId && activeRequest.id===request.id) activeRequest=null;
      if (["running","unknown"].includes(request.state)) {
        if (!draft && initialDraft===draft && request.input) draft=request.input.question;
        if (!activeRequest) activeRequest={desk:deskId,id:request.id,state:request.state,parentRequestId:request.input?.parent_request_id};
        else if (activeRequest.desk===deskId && activeRequest.id===request.id) activeRequest.state=request.state;
        ui().notify(request.state === "unknown" ? "The previous request has an unknown outcome and will not be retried automatically. Your question is preserved." : "The previous query is still executing; click Resume Query to get the results.");
        $('resume-query').hidden=false;
      }
    }
    const missing=activeRequest;
    if (missing?.desk===deskId && missing.kind!=='supplement' && !data.items.some(request=>request.id===missing.id))
      $('resume-query').hidden=false;
    if (resumeMissing && missing?.desk===deskId && missing.kind!=='supplement' && missing.state==='running'
        && missing.endpoint && missing.body && !data.items.some(request=>request.id===missing.id)) {
      await resumeMissingRequest(missing,deskId,epoch);
      if (!valid()) return;
    }
    for (const supplement of data.supplements || []) {
      const wasActive=activeRequest?.desk===deskId && activeRequest.id===supplement.id;
      if (supplement.state === 'running') {
        if (!activeRequest || (activeRequest.desk===deskId && activeRequest.id===supplement.id)) {
          activeRequest={desk:deskId,id:supplement.id,state:'running',kind:'supplement'};
          $('resume-query').hidden=false;$('cancel-question').hidden=false;
          ui().notify("The previous catch-up review is still being processed; viewing can be resumed or stopped. The original question has been retained.");
        }
        break;
      }
      if (supplement.state === 'unknown') {
        if (activeRequest?.desk===deskId && activeRequest.id===supplement.id) {
          activeRequest=null;$('resume-query').hidden=true;$('cancel-question').hidden=true;
        }
        if (wasActive || (draft && supplement.input?.question===draft))
          ui().notify("The result of the previous recheck is uncertain and will not be automatically reissued; the original question has been retained.");
        continue;
      }
      if (supplement.state === 'done' && activeRequest?.desk===deskId && activeRequest.id===supplement.id) {
        activeRequest=null;$('resume-query').hidden=true;$('cancel-question').hidden=true;
      }
      if (['failed','cancelled','waiting_budget'].includes(supplement.state) &&
          activeRequest?.desk===deskId && activeRequest.id===supplement.id) {
        activeRequest=null;$('resume-query').hidden=true;$('cancel-question').hidden=true;
        ui().notify(supplement.state==='waiting_budget'
          ? "The re-examination is pending budget or authorization; the original question has been retained and will not be automatically reissued."
          : "The previous recheck was not completed; the original question has been retained and will not be automatically reissued.");
        continue;
      }
      if (supplement.state === 'done' && !supplement.input) {
        if (wasActive) ui().notify("A previous recheck was completed, but the original issue record is missing; please check the retained draft.");
        continue;
      }
      if (supplement.state === 'done' && supplement.input && !activeRequest) {
        const original=supplement.input;
        const prepared=await api('/api/auto-chat/prepare','POST',original);
        if (!valid()) return;
        if (prepared.kind!=='ready' || prepared.engine!=='index') {
          ui().notify(prepared.message || "The recheck has been completed, but the answer cannot be confirmed; the original question has been retained.");
          continue;
        }
        const result=await api('/api/index-chat','POST',{...original,engine:'index'});
        if (!valid()) return;
        if (result?.response && !store().getSnapshot().records.some(r=>r.query.id===result.id)) {
          const parent=store().getSnapshot().records.find(r=>r.query.id===original.parent_request_id);
          ui().answer({...result,parentTurn:parent?.id || null},original.question);added=true;
          ui().notify("The supplementary inspection results have been restored.");
        }
      }
    }
    if ($('question').value !== initialDraft) draft=$('question').value;
    ui().restoreDraft(draft);
    if (added || draft!==initialDraft) {await flush();if (!valid()) return;}
  }
  async function resumeMissingRequest(pending,deskId,epoch) {
    const submission=submissionEpoch;
    const valid=()=>owns(deskId,epoch) && activeRequest===pending
      && submissionEpoch===submission
      && store().getSnapshot().epoch===pending.body.generation
      && currentVideo.id===pending.sourceId && currentVideo.generation===pending.sourceGeneration;
    if (!valid() || pending.recovering) return;
    pending.recovering=true;
    $('resume-query').hidden=false;
    ui().busy(true);
    ui().notify("Restoring using original request; no new request will be created.");
    try {
      let outcome;
      try {outcome=await api(pending.endpoint,'POST',pending.body);}
      catch(error) {
        if(error.data?.kind==='query_control' || error.data?.response) outcome=error.data;
        else throw error;
      }
      if (!valid()) return;
      if (outcome?.kind==='query_control' && outcome.request_id===pending.id && ['pending','unknown'].includes(outcome.state)) {
        pending.state=outcome.state==='unknown' ? 'unknown' : 'running';
        $('resume-query').hidden=false;$('cancel-question').hidden=false;
        ui().notify(outcome.suggested_action || "The original request is still being processed, please check again later.");
        status(pending.state==='unknown' ? "The call result is uncertain" : "Looking for evidence…",pending.state==='unknown' ? 'error' : '');
        return;
      }
      if (outcome?.id!==pending.id || outcome.mode!=='real' || outcome.video_id!==pending.sourceId
          || outcome.response?.schema_version!=='0.1'
          || (outcome.source_generation!==undefined && outcome.source_generation!==pending.sourceGeneration))
        throw new Error('Invalid recovery response');
      if (!isEmptyCancellation(outcome) && !isQueryResult(outcome,pending)) throw new Error('Invalid recovery response');
      activeRequest=null;
      $('resume-query').hidden=true;$('cancel-question').hidden=true;
      if (!isEmptyCancellation(outcome) && !store().getSnapshot().records.some(r=>r.query.id===outcome.id)) {
        ui().answer({...outcome,parentTurn:pending.parentTurn},pending.body.question);
      }
      ui().notify(isEmptyCancellation(outcome) ? "The original request has been canceled and the input has been retained." : "The original request result has been restored.");
      await flush();
    } catch(error) {
      if (valid()) {
        $('resume-query').hidden=false;
        ui().notify(`The original requested recovery is not completed:${error.message}. The input is preserved; the same request can be resumed again.`);
      }
    } finally {
      pending.recovering=false;
      if (owns(deskId,epoch) && submission===submissionEpoch && (!activeRequest || activeRequest===pending)) ui().busy(false);
    }
  }
  async function ask(question) {
    if (suspended || !current || !sourceReady(currentVideo)) return;
    if (activeRequest?.state!=="unknown" && activeRequest) {
      $('resume-query').hidden=false;ui().notify("The query is still being executed. Please resume the current request and do not initiate repeated attempts.");return;
    }
    const desk=current,epoch=requestEpoch,submission=++submissionEpoch;
    const context=ui().currentContext();
    const parentTurn=context?.mode==="real" ? store().getSnapshot().records.find(r=>r.query.id===context.id)?.id || null : null;
    const body={investigation_id:desk.id,request_id:crypto.randomUUID().replaceAll("-",""),
      generation:store().getSnapshot().epoch,question,parent_request_id:context?.mode==="real" ? context.id : null};
    let engine=currentVideo.knowledge_status?.required_for_chat ? 'auto' : ($('chat-engine').value || 'auto');
    let indexMode=false,localMode=false,cloudMode=false,endpoint;
    ui().busy(true);
    let pending=null;
    try {
      ui().notify("Question received, checking...");
      await flush(); if (!owns(desk.id,epoch)) return;
      if (activeRequest) {await stopRequest();if (!owns(desk.id,epoch)) return;}
      let prepared, partialNotice='';
      if(engine==='auto') {
        prepared=await api('/api/auto-chat/prepare',"POST",body);
        if(!owns(desk.id,epoch)) return;
        if(!['index','local','cloud','model'].includes(prepared.engine)) throw new Error('Invalid processing route; the question was not sent');
        engine=prepared.engine;
        ui().notify(prepared.route_message || "Processing method selected, checking...");
      }
      if (prepared?.has_index_matches && ['budget_blocked','supplement_unknown'].includes(prepared.kind)) {
        partialNotice=prepared.message+' Existing index results will be shown.';
        prepared={...prepared,kind:'ready'};
      }
      if(prepared?.kind==='supplement_required') {
        pending={desk:desk.id,id:body.request_id,state:'running',kind:'supplement'};activeRequest=pending;
        $('cancel-question').hidden=false;
        ui().notify(Number.isSafeInteger(prepared.estimated_new_calls)
          ? `Relevant pictures are being checked, at most ${prepared.estimated_new_calls} Cloud requests...`
          : "Checking the relevant pictures...");
        const started=await api('/api/knowledge-chat/supplement','POST',body);
        if(!owns(desk.id,epoch) || activeRequest!==pending)return;
        let supplement=started;
        for(let i=0;i<1800 && supplement.state==='running';i++) {
          await new Promise(r=>setTimeout(r,2000));
          if(!owns(desk.id,epoch) || activeRequest!==pending)return;
          supplement=await api(`/api/investigations/${desk.id}/supplements/${body.request_id}`);
        }
        if(!owns(desk.id,epoch) || activeRequest!==pending)return;
        if(supplement.state!=='done') {
          activeRequest=null;pending=null;
          const message=supplement.state==='waiting_budget'
            ? 'The supplementary check needs budget approval. Your question was kept and will not be resent automatically.'
            : 'The supplementary check did not finish or its result is unknown. Your question was kept and will not be resent automatically.';
          if (!prepared.has_index_matches || !['waiting_budget','failed','unknown'].includes(supplement.state)) {
            ui().notify(message);status("Supplementary inspection is not completed",'error');return;
          }
          partialNotice='The supplementary check did not finish or is unknown. It will not be retried automatically; existing index results follow.';
          prepared={...prepared,kind:'ready'};
        } else {
          activeRequest=null;pending=null;
          prepared=await api('/api/auto-chat/prepare','POST',body);
          if(!owns(desk.id,epoch))return;
          if(prepared.kind==='supplement_required') {
            prepared={...prepared,kind:'clarification_required',message:'Relevant frames were checked, but the full conditions remain unconfirmed. Your question was kept and the check will not be repeated automatically.'};
          }
          engine=prepared.engine;
        }
      }
      indexMode=engine==='index';localMode=engine==='local';cloudMode=engine==='cloud';
      if(indexMode)body.engine='index';
      endpoint=cloudMode ? '/api/cloud-chat' : localMode ? '/api/local-chat' : indexMode ? '/api/index-chat' : '/api/chat';
      if(!prepared)prepared=await api(`${endpoint}/prepare`,"POST",body);
      if(!owns(desk.id,epoch)) return;
      if (prepared.kind!=="ready") {
        preflightNotice=true;ui().notify(prepared.message);
        ui().preflightFeedback?.(prepared.message);return;
      }
      preflightNotice=false;
      pending={desk:desk.id,id:body.request_id,state:"running",parentTurn,parentRequestId:body.parent_request_id,
        endpoint,body:{...body},sourceId:currentVideo.id,sourceGeneration:currentVideo.generation};activeRequest=pending;
      $("cancel-question").hidden=false;
      status("Looking for evidence…");
      let data;
      for (let i=0;i<15 || localMode || cloudMode;i++) {
        if(activeRequest!==pending)return;
        try {data=await api(endpoint,"POST",body);}catch(error){if(error.data?.kind==="query_control") {data=error.data;}else if(error.data?.response) {data=error.data;} else throw error;}
        if(activeRequest!==pending)return;
        if(data?.response) activeRequest=null;
        if(!owns(desk.id,epoch)) return;
        if(data.kind!=="query_control") break;
        if(localMode && data.suggested_action) ui().notify(data.suggested_action);
        if(data.state==="unknown") {pending.state="unknown";$('resume-query').hidden=false;ui().notify(data.suggested_action);status("The call result is uncertain","error");return;}
        await new Promise(r=>setTimeout(r,Math.min(10000,3000+i*500)));
        if (!owns(desk.id,epoch) || activeRequest!==pending) return;
      }
      if(data?.response) {
        $('resume-query').hidden=true;
        if (!store().getSnapshot().records.some(r=>r.query.id===data.id)) ui().answer({...data,parentTurn},question);
        ui().notify(partialNotice);await flush();if (!owns(desk.id,epoch)) return;
      } else {$('resume-query').hidden=false;ui().notify("The query is still executing, leaving the original question intact. Please resume from the current request and do not initiate repeated attempts.");}
    }catch(error){if(owns(desk.id,epoch) && submission===submissionEpoch){if(activeRequest===pending && pending?.kind==='supplement'){activeRequest=null;pending=null;}else if(activeRequest===pending && pending) $('resume-query').hidden=false;ui().notify(error.message === "context_has_no_verified_anchor"
      ? "The selected question does not have valid evidence, please click \"New Question\" and write a complete question. Input reserved."
      : `${error.message} · The original question has been retained.`);status("Find not completed","error");}}
    finally {if(owns(desk.id,epoch) && submission===submissionEpoch && (!activeRequest || activeRequest===pending)) {$("cancel-question").hidden=true;ui().busy(false);refreshChatBudget();}}
  }
  async function cancelCurrent() {
    const pending=activeRequest,desk=current?.id,epoch=requestEpoch,deskEpoch=store().getSnapshot().epoch,submission=submissionEpoch;
    if(!pending)return;
    const control=$("cancel-question"), cancellation=++cancelEpoch;control.disabled=true;
    let operationSubmission=submission;
    try {
      const outcome=await stopRequest();
      if(!owns(desk,epoch) || store().getSnapshot().epoch!==deskEpoch || submission!==submissionEpoch)return;
      submissionEpoch++;
      const cancelledSubmission=submissionEpoch;
      operationSubmission=cancelledSubmission;
      control.hidden=true;
      const completed=outcome?.state==='done' || isQueryResult(outcome,pending);
      const unknown=outcome?.state==='unknown';
      const failed=['failed','waiting_budget'].includes(outcome?.state);
      if (completed && pending.kind==='supplement') {
        ui().busy(true);
        ui().notify("The recheck was completed before stopping, results are being restored...");
        try { await recover(); }
        catch { if (owns(desk,epoch)) ui().notify("The supplementary investigation has been completed, but the recovery failed; reopen the investigation to view it again. Input reserved."); }
        finally { if (owns(desk,epoch) && cancelledSubmission===submissionEpoch) ui().busy(false); }
        return;
      }
      ui().busy(false);
      ui().notify(completed && pending.kind!=='supplement' ? "The search has ended with results and input retained."
        : unknown ? "The recheck result is uncertain and will not be automatically resent; the input has been reserved."
        : failed ? "The catch-up check was not completed before stopping; the input has been retained and will not be automatically resent."
        : "This round of processing has been stopped and the input has been retained. You can modify it and resend it.");
      status(completed ? (pending.kind==='supplement' ? "Supplementary inspection completed" : "Search ended") : unknown ? "The result is uncertain" : failed ? "Supplementary inspection is not completed" : "Processing has stopped");await flush();
    }catch(error){if(owns(desk,epoch) && operationSubmission===submissionEpoch)ui().notify("Stop incomplete:"+error.message+";The input has been reserved and can be clicked again to stop.");}
    finally{if(cancellation===cancelEpoch)control.disabled=false;}
  }
  function isQueryResult(outcome,pending) {
    return pending?.kind!=='supplement' && outcome?.id===pending?.id && outcome.mode==='real'
      && outcome.video_id===currentVideo.id && outcome.response?.schema_version==='0.1'
      && (outcome.source_generation===undefined || outcome.source_generation===currentVideo.generation)
      && ['ok','partial','no_result','error'].includes(outcome.response.status)
      && !isEmptyCancellation(outcome);
  }
  function isEmptyCancellation(result) {
    const response=result?.response;
    return response?.status==='error' && Array.isArray(response.events) && !response.events.length
      && Array.isArray(response.errors) && response.errors.length>0
      && response.errors.every(error=>error?.code==='query_cancelled');
  }
  async function stopRequest() {
    const pending=activeRequest;
    if(!pending) return;
    const epoch=requestEpoch, deskEpoch=store().getSnapshot().epoch;
    const kind=pending.kind==='supplement' ? 'supplements' : 'requests';
    const outcome=await api(`/api/investigations/${pending.desk}/${kind}/${pending.id}/cancel`,"POST");
    if (activeRequest===pending && owns(pending.desk,epoch)) {
      activeRequest=null;
      if (store().getSnapshot().epoch===deskEpoch) {
        if (isQueryResult(outcome,pending) && !store().getSnapshot().records.some(r=>r.query.id===outcome.id)) {
          const parent=store().getSnapshot().records.find(r=>r.query.id===pending.parentRequestId);
          ui().answer({...outcome,parentTurn:pending.parentTurn || parent?.id || null});
        }
        $('cancel-question').hidden=true;
        $('resume-query').hidden=true;
      }
    }
    return outcome;
  }
  async function start() {
    if(!ui()?.ready())return;
    try {
      session=await api("/api/session");
      let id;try{id=localStorage.getItem("frameseek-current");}catch{}
      const desks=(await api("/api/investigations")).items;
      suspended=false;
      if(id && desks.some(d=>d.id===id))await openDesk(id);
      else if(desks.length)await openDesk(desks[0].id);else await create(demo);
      if(/(?:^|[?&])demo=basketball(?:&|$)/.test(window.location?.search || '')) {
        if(currentVideo.id!==demo.id) await openSource(demo);
        window.requestAnimationFrame?.(focusDemoQuestion);
      }
      store().subscribe(changed);
      window.addEventListener("frameseek-canvas-changed",changed);
      window.addEventListener("frameseek-explicit-seek",changed);
      ["pointerup","keyup","wheel","input"].forEach(event=>document.addEventListener(event,()=>{if(JSON.stringify(snapshot())!==lastSaved)changed();},{passive:true}));
      $('question').addEventListener('input',()=>{keepDraftShadow();if(preflightNotice){preflightNotice=false;ui().notify('');}});
      $('knowledge-control').onclick=async()=>{
        const video=currentVideo,deskId=current?.id,epoch=requestEpoch,control=$('knowledge-control');
        if(!video?.knowledge_status?.required_for_chat || control.disabled)return;
        const cancelling=['queued','indexing'].includes(video.knowledge_status.state);
        control.disabled=true;
        try {
          const status=await api(`/api/videos/${video.id}/knowledge/${cancelling?'cancel':'build'}`,'POST');
          if(!owns(deskId,epoch))return;
          currentVideo={...video,knowledge_status:status};ui().updateKnowledge?.(currentVideo);
          ui().notify(cancelling?"Indexing stopped, fragment retention completed.":"Base indexing in progress; video still playable.");
          if(!cancelling)watch(video.id,epoch);
        } catch(error) {if(owns(deskId,epoch))ui().notify(`${error.message} · Video and records have been retained.`);}
        finally {if(owns(deskId,epoch))control.disabled=false;}
      };
      document.addEventListener("visibilitychange",()=>{if(document.hidden)flush().catch(()=>{});});
      $("open-investigations").onclick=()=>library().catch(e=>ui().notify(e.message));
      $("open-sources").onclick=()=>library("videos").catch(e=>ui().notify(e.message));
      $("open-agent-setup").disabled=false;
      $("open-agent-setup").onclick=showRuntime;
      $("refresh-agent-setup").onclick=showRuntime;
      $("close-agent-setup").onclick=()=>$("agent-setup-dialog").close();
      $('search-index').onclick=searchSavedFrames;
      $('chat-engine').onchange=()=>{syncSourceSwitcher();refreshChatBudget();};
      $('close-visual-search').onclick=()=>{searchEpoch++;$('visual-search-dialog').close();};
      $("save-state").onclick=()=>conflict ? openDesk(current.id).catch(e=>ui().notify(e.message)) : flush().catch(()=>{});
      $("cancel-question").onclick=()=>cancelCurrent();
      $("resume-query").onclick=()=>recover({resumeMissing:true}).catch(e=>ui().notify(e.message));
      $("source-switcher").onchange=switchSelectedSource;
      refreshSourceSwitcher().catch(error=>ui().notify(`Failed to read video list:${error.message}. You can try again in the management video.`));
      $("close-library").onclick=()=>$("workspace-library").close();
      $("delete-cancel").onclick=()=>$("delete-dialog").close();
      document.body?.removeAttribute("data-workspace-startup");
    }catch(error){suspended=true;status("Investigation service unavailable","error");ui().notify(error.message);document.body?.setAttribute("data-workspace-startup","error");}
  }
  window.FrameSeekPersistence={flush,upload,ask,library,searchIndex:searchSavedFrames,canSearchOcr,openDemo:()=>openSource(demo).then(opened=>{
    if(opened) window.requestAnimationFrame?.(focusDemoQuestion);
    return opened;
  }).catch(e=>ui().notify(e.message)),
    newInvestigation:()=>create(currentVideo).catch(e=>ui().notify(e.message))};
  const startupRetry=$("startup-retry");
  if(startupRetry)startupRetry.onclick=()=>window.location.reload();
  window.addEventListener("frameseek-catalog-error",()=>document.body?.setAttribute("data-workspace-startup","error"),{once:true});
  window.addEventListener("frameseek-catalog-ready",start,{once:true});
  if(ui()?.ready())start();
})();
