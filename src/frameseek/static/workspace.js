/* Durable investigations, owned sources, and recoverable real-model requests. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const ui = () => window.FrameSeekSessionUI;
  const store = () => window.FrameSeekWorkbench;
  const demo = {id:"demo_parcel",name:"包裹移动 · 合成演示",metadata:{duration:18,audio:false},job:{state:"ready"}};
  const labels = { queued:"等待预检", probing:"检查素材", captioning:"理解画面", transcribing:"转写音轨", indexing:"建立索引", ready:"可以提问", failed:"处理失败", cancelled:"已取消", awaiting_configuration:"等待模型配置" };
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
    if (!knowledge?.required_for_chat) return labels[video.job?.state] || '已上传';
    const states={waiting_probe:'等待视频预检',waiting_processor:'等待索引配置',waiting_budget:'等待预算或授权',queued:'等待建立索引',indexing:'建立基础索引',ready:'可以提问',cancelled:'索引已停止',failed:'索引失败',unknown:'索引状态待确认',not_started:'等待建立索引'};
    const status=states[knowledge.state] || '索引尚未就绪';
    return knowledge.state==='indexing' && knowledge.total_segments ? `${status} · ${knowledge.completed_segments}/${knowledge.total_segments} 段` : status;
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

    $('auto-engine-indicator').textContent=$('chat-engine').value==='auto' ? '自动' : '手动';
    $('chat-engine').parentElement && ($('chat-engine').parentElement.hidden=currentVideo.id===demo.id);
    $('search-index').hidden = currentVideo.id === demo.id || !sourceReady(currentVideo);
    if(currentVideo.id!==demo.id && sourceReady(currentVideo)) $('composer-mode-text').textContent=currentVideo.knowledge_status?.required_for_chat ? (currentVideo.knowledge_status.processor_available===false ? '已有索引可查询 · 本机补查暂不可用' : currentVideo.knowledge_status.processor==='synthetic_fixture' ? '合成测试索引 · 模拟补查' : currentVideo.knowledge_status.processor==='tokenfactory' ? '基础索引 · 云端补查需预算准入' : '本机索引 · 按需补查') : $('chat-engine').value==='auto' ? 'Agent 自动选择 · 云端处理仍需预算准入' : $('chat-engine').value==='local' ? '本机视觉 · 免费' : $('chat-engine').value==='index' ? '已有索引 · 零模型调用' : $('chat-engine').value==='cloud' ? '云端视觉 · 需预算准入' : '模型分析 · 需预算准入';
    if(searchDesk && (searchDesk !== current?.id || searchGeneration !== currentVideo.generation)) {
      searchEpoch++; searchDesk=null; $('visual-search-dialog').close(); $('visual-search-results').replaceChildren();
    }
    if (videos) sourceChoices = new Map([[demo.id, demo], ...videos.map(video => [video.id, video])]);
    sourceChoices.set(currentVideo.id, currentVideo);
    const select = $("source-switcher");
    select.replaceChildren();
    for (const video of sourceChoices.values()) {
      const option = document.createElement("option");
      option.value = video.id; option.textContent = video.id === demo.id ? "箱子 · 包裹移动（合成演示）" : video.name;
      option.dataset.thumbnail = video.id === demo.id ? "/media/demo_parcel/frames/0.jpg" : video.metadata?.preview?.[0]?.uri || "";
      const duration = Math.round(video.metadata?.duration || 0);
      option.dataset.detail = `${Math.floor(duration / 60).toString().padStart(2,"0")}:${(duration % 60).toString().padStart(2,"0")} · ${video.id === demo.id ? "合成演示" : sourceStatus(video)}`;
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
    catch (error) { ui().notify(`切换视频失败：${error.message}。当前调查和输入已保留。`); }
    finally { sourceSwitching = false; syncSourceSwitcher(); }
  }

  function status(text, kind="") { $("save-state").textContent = kind === "saved" ? "已保存 ✓" : text; $("save-state").title = text; $("save-state").dataset.state = kind; }
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
    if(!session?.query_reference_snapshots || currentVideo.id===demo.id)return state;
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
    status("正在保存…");
    pendingSave={desk:desk.id,revision:desk.revision,draft:state.ui?.draft || ''};
    keepDraftShadow();
    saving = api(`/api/investigations/${desk.id}`, "PUT", {revision:desk.revision,state:wireSnapshot(JSON.parse(encoded))})
      .then(result => { desk.revision=result.revision; lastSaved=encoded; lastSavedEdits=edits; status("已保存到本机","saved");
        if(current===desk) {
          if($('question').value===(state.ui?.draft || '')) {try{sessionStorage.removeItem(draftShadowKey);}catch{}}
          else keepDraftShadow(state.ui?.draft || '');
        }
      })
      .catch(error => { conflict=error.status===409; status(conflict ? "其他页面已修改 · 请重新打开" : "保存失败 · 点击重试", "error"); throw error; })
      .finally(() => { saving=null; pendingSave=null; });
    await saving;
    if (current!==desk) return;
    if (editSnapshot(snapshot())!==lastSavedEdits) return flush();
  }
  function changed() {
    if (suspended || conflict) return;
    status("有未保存修改"); clearTimeout(timer);
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
      const video=desk.video === "demo_parcel" ? demo : await api(`/api/videos/${desk.video}`);
      if (!owns(previousId,epoch)) return false;
      await flush(); if (!owns(previousId,epoch)) return false;
      if (desk.id===previousId && !conflict && current.revision>desk.revision) {desk.state=snapshot();desk.revision=current.revision;}
      suspended=true;
      current=desk; currentVideo=video; conflict=false;
      ui().restore({...desk.state,title:desk.state.title || desk.title},video);
      const restored=snapshot();lastSaved=JSON.stringify(restored);lastSavedEdits=editSnapshot(restored);
      remember(); syncSourceSwitcher(); status("已恢复调查","saved");
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
      const desk=await api("/api/investigations","POST",{video_id:video.id,title:video.id === "demo_parcel" ? "未命名调查" : video.name.replace(/\.[^.]+$/, "")});
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
  function item(title, detail, action, actionLabel="打开") {
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
    if(currentVideo.knowledge_status?.required_for_chat) {line.hidden=false;line.textContent=currentVideo.knowledge_status.processor_available===false?'已有索引可查询；本机补查暂不可用。':'自动读取基础索引；需要时只补查相关画面。';return;}
    if(currentVideo.id==='demo_parcel') {line.hidden=true;return;}
    if($('chat-engine').value==='auto') {line.hidden=false;line.textContent='先检查已有证据，必要时选择可用视觉分析；云端处理仍受原预算限制，失败不会自动切换重试。';return;}
    if($('chat-engine').value==='local') {
      line.hidden=false;line.textContent='本机视觉识别，不使用 Token Factory 额度。首次扫描全片，完成的片段会缓存；不会自动调用付费 API。';return;
    }
    try {
      const data=await api('/api/runtime/status');
      if(check!==budgetEpoch || !owns(desk,epoch))return;
      const b=data.budget;
      line.hidden=false;
      const prefix=$('chat-engine').value==='index' ? '当前索引问答不调用模型，可继续检索已有数据。' : '';
      line.textContent=prefix+(b && b.remaining_calls===0
        ? `模型请求已用完 ${b.call_cap} / ${b.call_cap} 次；不会再调用模型，已有回答和证据仍可回看。`
        : !data.dispatch_prerequisites_met
        ? '真实聊天暂未就绪，可在「更多 → 模型与预算」检查；已有证据仍可回看。'
        : b?.visual_search_calls === 1 ? `剩余 ${b.remaining_calls} / ${b.call_cap} 次模型请求 · 视觉查找通常预留1次，复核最多8张索引候选；不代表已穷举全片。`
        : b ? `剩余 ${b.remaining_calls} / ${b.call_cap} 次模型请求 · 指定不超过60秒的时段通常预留 ${b.short_window_calls} 次；跨时段检索需要更多。`
        : '预算状态未确认，请先检查模型与预算；不代表可以发送。');
    }catch(error){if(check===budgetEpoch && owns(desk,epoch)){line.hidden=false;line.textContent='预算状态读取失败，发送前仍需通过账本检查；不会自动调用模型。';}}
  }
  async function showRuntime() {
    const dialog=$("agent-setup-dialog"), button=$("refresh-agent-setup");
    if(!dialog.open) dialog.showModal();
    button.disabled=true;$("agent-setup-summary").textContent="正在读取本机配置…";
    try {
      const data=await api("/api/runtime/status");
      const provider=data.provider==="gemini" ? "Gemini · AI Builder Cup" : data.provider==="tokenfactory" ? "NVIDIA Nemotron · Token Factory" : "NVIDIA / Nebius";
      $("agent-setup-summary").textContent=data.dispatch_prerequisites_met
        ? `${provider}：聊天准入条件齐备，已有索引的视频可直接提问，无需重新分析。检查仅读取配置和账本，不能证明每个回答准确。`
        : `${provider}：真实分析尚未就绪。演示、上传预检、播放和调查保存仍可使用。`;
      const list=$("agent-readiness-checks");list.replaceChildren();
      for(const check of data.checks) {
        const row=document.createElement("article");row.className="agent-check";
        const heading=document.createElement("strong");heading.textContent=check.label;
        const state=document.createElement("span");state.textContent=check.state==="available" ? "已提供" : "待准备";state.dataset.state=check.state;
        const detail=document.createElement("p");detail.textContent=check.detail;
        row.append(heading,state,detail);list.append(row);
      }
      const models=$("agent-model-roles");models.replaceChildren();
      for(const model of data.models) {
        const row=document.createElement("p");row.textContent=`${model.label}${model.optional ? "（可选）" : ""} · ${model.configured ? `${model.model} / ${model.revision} · ${model.key_present ? "凭据已配置" : "缺少凭据"}` : "未配置"}`;
        models.append(row);
      }
    }catch(error){$("agent-setup-summary").textContent=`读取失败：${error.message}。可重新检查。`;}
    finally {button.disabled=false;}
  }
  async function searchSavedFrames(savedQuestion=null) {
    const question=typeof savedQuestion==='string' ? savedQuestion.trim() : typeof savedQuestion?.question==='string' ? savedQuestion.question.trim() : $('question').value.trim();
    const answerRequestId=typeof savedQuestion?.answerRequestId==='string' ? savedQuestion.answerRequestId : null;
    let autoOcr=!!savedQuestion?.autoOcr;
    if(!question) {ui().notify('先在输入框描述要找的内容，再查找已有画面；不会发送给模型。');return;}
    const desk=current?.id, epoch=requestEpoch, source=currentVideo, generation=source.generation, check=++searchEpoch;
    if(!desk || source.id===demo.id || !sourceReady(source))return;
    const dialog=$('visual-search-dialog'), list=$('visual-search-results');
    searchDesk=desk; searchGeneration=generation; list.replaceChildren();
    $('visual-search-summary').textContent='正在检索已有索引，不调用模型…';
    if(!dialog.open)dialog.showModal();
    let offset=0, snapshot=null, displayed=0, selectedConditions=null, filterRevision=0, candidateSection=null;
    const active=()=>check===searchEpoch && owns(desk,epoch) && currentVideo.id===source.id && currentVideo.generation===generation && dialog.open;
    $('visual-search-conditions').textContent='';$('visual-search-coverage').textContent='';
    $('visual-search-track').replaceChildren();$('visual-search-track').hidden=true;
    $('visual-search-options').replaceChildren();$('visual-search-filters').hidden=true;
    $('visual-search-apply').onclick=()=>{
      if(!active())return;
      selectedConditions=[...$('visual-search-options').children].filter(row=>row.firstElementChild.checked).map(row=>JSON.parse(row.dataset.condition));
      if(!selectedConditions.length){ui().notify('请至少选择一个明确条件。');return;}
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
            ? '文字候选未运行：请先按提示明确检索条件；已有索引未改变，未调用模型。'
            : '未执行新的视觉识别；明确条件后可继续检索已有索引。';return;
        }
        if(button)button.remove();
        const structured=data.kind==='indexed_occurrences', rows=structured ? data.occurrences : data.frames;
        displayed+=rows.length;
        $('visual-search-conditions').textContent=data.conditions?.length ? `${data.explicit_conditions ? "已选属性（替代原问题）" : "全部条件"}：${data.conditions.map(c=>`${c.field} ${c.operator==='is_not' ? '不是' : c.operator==='contains' || c.operator==='color_item' ? '包含' : '是'} ${c.value}`).join('；')}（同一物体）` : `检索概念：${(data.terms || []).join(' + ')}`;
        const coverage=data.coverage || {};
        $('visual-search-summary').textContent=structured
          ? `已有观察匹配 ${displayed} / ${data.total_occurrences} ${coverage.catalog_complete === false ? '个抽样画面时点（不是人数或事件次数）' : '个区间或时点'}；本次模型调用 0 次。`
          : `已检索全部 ${data.indexed_frames} 张保存画面的描述，已显示 ${displayed} / ${data.total_candidates ?? rows.length} 张文字候选。候选不等于满足全部条件；不是整段视频的全部出现时间。没有新增模型调用。`;
        $('visual-search-coverage').textContent=structured
          ? `${data.provenance} · 已检索 ${coverage.reviewed_frames} / ${coverage.catalog_frames} 张索引画面的观察；缺少观察 ${coverage.missing_frame_ids?.length || 0} 张，不确定 ${coverage.uncertain_frame_ids?.length || 0} 张。${coverage.catalog_complete === false ? '索引仅包含抽样画面，并非逐帧检查。' : ''}${coverage.exhaustive_under_index ? '已遍历索引内全部源帧；识别准确性依赖原观察。' : '覆盖未完成，不能断言全部出现时间或全片不存在。'}`
          : '缺少全片结构化观察索引。以下只检索已有描述，不能确认同一主体的多个属性，也不能推断连续出现区间。';
        if(autoOcr && (data.ocr_eligible!==true || !canSearchOcr(question))) {
          autoOcr=false;
          $('visual-search-coverage').textContent+=!currentVideo?.knowledge_status?.ocr_candidates_available
            ? ' 文字候选未运行：当前服务未启用 OCR。未调用模型。'
            : ' 文字候选不可用：目前只支持英文和数字；该时段也可能没有可用抽样画面，或问题包含不支持的条件。未调用模型。';
        }
        if(!rows.length) {const empty=document.createElement('p');empty.textContent=coverage.exhaustive_under_index ? '当前已有观察中没有匹配。' : '当前索引未找到匹配；未检查范围仍可能出现。';list.append(empty);}
        if(!offset && !selectedConditions && data.ocr_eligible===true && canSearchOcr(question)) {
          const ocr=document.createElement('button');ocr.type='button';ocr.textContent='查找此时段的文字候选 · OCR';
          ocr.onclick=async()=>{
            if(!currentResult())return;
            ocr.disabled=true;$('visual-search-summary').textContent='正在查找文字候选…';
            try {
              const candidates=await api(`/api/videos/${source.id}/ocr-candidates`,'POST',{question,generation});
              if(!currentResult())return;
              if(!data.knowledge_version || candidates.knowledge_version!==data.knowledge_version) {
                ocr.remove();
                $('visual-search-summary').textContent='索引版本已变化或无法确认；旧回答与新文字候选未混合。请从当前视频重新查询最新索引。';
                const latest=document.createElement('button');latest.type='button';latest.textContent='查看最新索引';
                latest.onclick=()=>{if(currentResult())searchSavedFrames({question});};
                list.append(latest);
                return;
              }
              ocr.remove();
              const observed=new Set(structured ? rows.flatMap(entry=>(entry.evidence || []).map(frame=>frame.frame_id).filter(Boolean)) : rows.map(frame=>frame.frame_id).filter(Boolean));
              const newCandidates=candidates.frames.filter(frame=>!observed.has(frame.frame_id));
              $('visual-search-summary').textContent=`已有索引结果保留；OCR 另找到 ${newCandidates.length} 张未核实候选画面，请自行回看。`;
              $('visual-search-coverage').textContent+=` OCR 检查 ${candidates.checked_frames} 张抽样画面，${candidates.failed_frames} 张处理失败；不能据此断言其他时间不存在。未调用云端模型。`;
              for(const frame of newCandidates) {
                if(!frame.uri.startsWith(`/media/${source.id}/frames/`))continue;
                const row=document.createElement('article'), image=document.createElement('img'), caption=document.createElement('p'), seek=document.createElement('button');
                image.src=frame.uri;image.alt='OCR 文字候选，需回看核实';image.loading='lazy';
                caption.textContent='OCR 候选 · 待核实，文字可能识别错误';
                seek.type='button';seek.textContent=`回看 ${Math.floor(frame.t/60).toString().padStart(2,'0')}:${(frame.t%60).toFixed(3).padStart(6,'0')} · 待核实`;
                seek.onclick=()=>{if(!currentResult())return;dialog.close();ui().seek(frame.t,seek);};
                row.append(image,caption,seek);list.append(row);
              }
            } catch(error) {if(currentResult())$('visual-search-summary').textContent='文字候选未完成。请指定不超过 8 秒的时间段和引号中的文字，或稍后重试；已有索引未改变。';}
            finally {ocr.disabled=false;}
          };list.append(ocr);
          if(autoOcr){autoOcr=false;await ocr.onclick();if(!currentResult())return;}
        }
        for(const entry of rows) {
          const frame=structured ? entry.evidence[0] : entry;
          if(!frame?.uri.startsWith(`/media/${source.id}/frames/`))continue;
          const row=document.createElement('article'), image=document.createElement('img');
          const caption=document.createElement('p'), seek=document.createElement('button');
          image.src=frame.uri; image.alt=structured ? '已有结构化观察的原视频证据' : '原视频已保存的候选画面';image.loading='lazy';
          caption.textContent=structured ? `同一画面主体满足全部条件 · ${entry.evidence_count} 帧支持；不代表跨镜头同一个人。` : frame.caption;
          const clock=t=>`${Math.floor(t/60).toString().padStart(2,'0')}:${(t%60).toFixed(3).padStart(6,'0')}`;
          seek.textContent=structured ? `回看 ${clock(entry.start)}${entry.end==null ? ' · 抽样时点' : `—${clock(entry.end)}`} · 已有观察` : `回看 ${clock(frame.t)} · 索引候选`;
          seek.type='button';seek.onclick=()=>{if(!currentResult())return;dialog.close();ui().seek(frame.t,seek);};
          if(structured && data.duration>0) {
            const marker=document.createElement('button');marker.type='button';
            marker.textContent='';marker.title=seek.textContent;marker.setAttribute('aria-label',`时间线 ${clock(entry.start)}`);
            marker.style.left=`${100*entry.start/data.duration}%`;
            marker.style.width=`${Math.max(.5,100*((entry.end ?? entry.start+.001)-entry.start)/data.duration)}%`;
            marker.onclick=seek.onclick;$('visual-search-track').append(marker);$('visual-search-track').hidden=false;
          }
          row.append(image,caption,seek);candidateSection ? list.insertBefore(row,candidateSection) : list.append(row);
        }
        if(!offset && structured && Array.isArray(data.uncertain_candidates) && data.uncertain_candidates.length) {
          candidateSection=document.createElement('section');candidateSection.className='uncertain-candidate-group';
          candidateSection.setAttribute('aria-label','待核实候选画面');
          const heading=document.createElement('p');
          heading.textContent=`待核实候选 · 共 ${data.uncertain_candidate_total} 张；部分条件不明确，不计入匹配结果。`;
          const controls=document.createElement('div'), timeInput=document.createElement('input'), jump=document.createElement('button');
          controls.className='candidate-time-controls';timeInput.type='text';timeInput.placeholder='例如 05:20';
          timeInput.setAttribute('aria-label','候选画面起始时间');
          jump.type='button';jump.textContent='跳到时间';controls.append(timeInput,jump);
          candidateSection.append(heading,controls);list.append(candidateSection);
          const candidateSnapshot=data.snapshot, candidateVersion=data.knowledge_version;
          let candidateEpoch=0;
          const appendCandidates=page=>{
            for(const frame of page.uncertain_candidates || []) {
              if(typeof frame.uri!=='string' || !frame.uri.startsWith(`/media/${source.id}/frames/`) || !Number.isFinite(frame.t) || frame.t<0 || frame.t>=data.duration)continue;
              const row=document.createElement('article'), image=document.createElement('img'), caption=document.createElement('p'), seek=document.createElement('button');
              image.src=frame.uri;image.alt='部分条件不明确的候选画面';image.loading='lazy';
              caption.textContent='候选画面 · 部分条件不明确，请回看核实';
              seek.type='button';seek.textContent=`回看 ${Math.floor(frame.t/60).toString().padStart(2,'0')}:${(frame.t%60).toFixed(3).padStart(6,'0')} · 待核实`;
              seek.onclick=()=>{if(!currentResult())return;dialog.close();ui().seek(frame.t,seek);};
              row.append(image,caption,seek);candidateSection.append(row);
            }
            if(page.uncertain_candidate_next_offset!=null) {
              const moreCandidates=document.createElement('button');moreCandidates.type='button';
              moreCandidates.textContent='加载更多待核实画面 · 不调用模型';
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
                } catch(error) {if(currentResult() && request===candidateEpoch)$('visual-search-coverage').textContent+=' 候选画面加载失败，请重新打开索引；未调用模型。';}
                finally {moreCandidates.disabled=false;}
              };candidateSection.append(moreCandidates);
            }
          };
          jump.onclick=async()=>{
            if(!currentResult())return;
            const match=timeInput.value.trim().match(/^(\d{1,3}):([0-5]\d)$/);
            const seconds=match ? Number(match[1])*60+Number(match[2]) : NaN;
            if(!Number.isFinite(seconds) || seconds>=data.duration) {heading.textContent='请输入视频范围内的时间，例如 05:20。';return;}
            const request=++candidateEpoch;jump.disabled=true;
            try {
              const page=await api(`/api/videos/${source.id}/occurrences/search`,'POST',{
                question,generation,offset:0,limit:40,snapshot:candidateSnapshot,conditions:selectedConditions,
                answer_request_id:answerRequestId,candidate_time:seconds
              });
              if(!currentResult() || request!==candidateEpoch)return;
              if(page.snapshot!==candidateSnapshot || page.knowledge_version!==candidateVersion)throw new Error('index_version_changed');
              candidateSection.replaceChildren(heading,controls);
              heading.textContent=`待核实候选 · 共 ${data.uncertain_candidate_total} 张；显示 ${timeInput.value.trim()} 起的画面，不计入匹配结果。`;
              if(!page.uncertain_candidates?.length) {const empty=document.createElement('p');empty.textContent='此时间之后没有待核实候选。';candidateSection.append(empty);}
              appendCandidates(page);
            } catch(error) {if(currentResult() && request===candidateEpoch)heading.textContent='跳转失败，请重新打开索引；未调用模型。';}
            finally {jump.disabled=false;}
          };
          appendCandidates(data);
        }
        if(data.next_offset!=null) {
          offset=data.next_offset;snapshot=data.snapshot;
          const more=document.createElement('button');more.type='button';
          more.textContent='加载后续时间 · 不调用模型';more.onclick=()=>currentResult() && loadPage(more);
          candidateSection ? list.insertBefore(more,candidateSection) : list.append(more);
        }
      } catch(error) {
        if(currentResult() && error.message==='index_answer_version_unavailable') {
          $('visual-search-summary').textContent='这条旧回答没有保存索引版本，不能确认原有依据。可查看最新索引；原回答和聊天不会改变。';
          const latest=document.createElement('button');latest.type='button';latest.textContent='查看最新索引';
          latest.onclick=()=>{if(currentResult())searchSavedFrames({question});};list.append(latest);
        } else if(currentResult())$('visual-search-summary').textContent=`索引暂不可用：${error.message}。原问题和聊天保留，未调用模型。`;
      }
      finally {if(button)button.disabled=false;}
    }
    await loadPage();
  }

  async function library(kind="investigations") {
    const deskId=current?.id, epoch=requestEpoch, token=++libraryEpoch;
    const active=()=>owns(deskId,epoch) && token===libraryEpoch;
    await flush(); if (!active()) return; libraryNotice("");
    $("library-title").textContent=kind === "videos" ? "素材库" : "调查库";
    $("library-intro").textContent=kind === "videos" ? "每段素材独立保存问题与证据。上传后先预检，再按已批准预算分析。" : `调查保存在这台电脑的服务中，默认保留 ${session.ttl_hours} 小时。继续上次的路径，或开启新调查。`;
    $("library-add").textContent=kind === "videos" ? "上传视频" : "新建调查";
    $("library-add").onclick=()=>{
      if(kind === "videos") {$("workspace-library").close();$("file-input").click();}
      else create(currentVideo).catch(e=>libraryNotice(e.message));
    };
    const body=$("library-items");body.replaceChildren();
    $("workspace-library").showModal();
    if (kind === "videos") {
      body.append(item(demo.name,"18 秒 · 公开合成演示 · 预设回答",()=>openSource(demo)));
      const data=await api("/api/videos");
      if (!active()) return;
      if (data.items.some(video=>video.knowledge_status?.required_for_chat)) $("library-intro").textContent="每段素材独立保存问题与证据。上传后先预检并建立基础索引，完成后可以提问。";
      syncSourceSwitcher(data.items);
      for (const video of data.items) {
        const job=video.job;
        const row=item(video.name,`${Math.round(video.bytes/1024/1024)} MB · ${sourceStatus(video)}${!video.knowledge_status?.required_for_chat && job?.total ? ` · ${job.completed}/${job.total} 窗口` : ""}`,()=>openSource(video));
        if (["failed","cancelled"].includes(job?.state) && !video.metadata?.windows) {
          const retry=document.createElement("button");retry.className="outline-button";retry.textContent="重新预检";
          retry.onclick=async()=>{try {await api(`/api/videos/${video.id}/retry`,"POST");libraryNotice("已重新开始预检。");if(currentVideo.id===video.id) watch(video.id,requestEpoch);}catch(e){libraryNotice(e.message);}};
          row.append(retry);
        } else if (["awaiting_configuration","failed","cancelled"].includes(job?.state)) {
          const analyze=document.createElement("button");analyze.className="outline-button";analyze.textContent="开始分析";
          analyze.disabled=!session.real_ready || !video.metadata?.windows;
          analyze.title=analyze.disabled ? "需要模型服务、有效校准记录和已批准预算" : "使用已批准预算分析完整素材";
          analyze.onclick=async()=>{try {await api(`/api/videos/${video.id}/analyze`,"POST"); libraryNotice("已受理分析。点击刷新查看真实阶段。"); if (currentVideo.id===video.id) watch(video.id,requestEpoch);}catch(e){libraryNotice(e.message);}};
          row.append(analyze);
        }
        if (["queued","probing","captioning","transcribing","indexing"].includes(job?.state)) {
          const cancel=document.createElement("button");cancel.className="text-button";cancel.textContent="取消";
          cancel.onclick=async()=>{await api(`/api/videos/${video.id}/cancel`,"POST");await library("videos");};row.append(cancel);
        }
        const rename=document.createElement("button");rename.className="text-button";rename.textContent="重命名";
        rename.onclick=()=>renameSource(video);row.append(rename);
        const remove=document.createElement("button");remove.className="text-button danger";remove.textContent="删除";
        remove.setAttribute("aria-label",`删除素材：${video.name}`);
        remove.onclick=()=>confirmDelete(video,"videos");row.append(remove);body.append(row);
      }
      if (!session.real_ready && !data.items.some(video=>video.knowledge_status?.required_for_chat)) libraryNotice("真实分析等待模型地址与预算配置。上传、预检、播放和调查保存已可使用。");
    } else {
      const data=await api("/api/investigations");
      if (!active()) return;
      for (const desk of data.items) {
        const row=item(desk.title,`${desk.video === "demo_parcel" ? "演示素材" : "真实素材"} · ${new Date(desk.updated*1000).toLocaleString()}${desk.id===current?.id ? " · 当前" : ""}`,()=>openDesk(desk.id));
        const remove=document.createElement("button");remove.className="text-button danger";remove.textContent="删除";
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
    $("delete-description").textContent=table === "videos" ? `删除「${entity.name}」及其所有调查、证据和原文件？` : `删除调查「${entity.title}」？`;
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
  async function upload(file) {
    if (!file.type.startsWith("video/") || file.size > 500*1024**2) {ui().notify("请选择不超过 500 MB 的视频文件。");return;}
    try {
      const deskId=current?.id, epoch=requestEpoch;
      await flush();if (!owns(deskId,epoch)) return;ui().notify(`正在上传 ${file.name} · 0%`);
      const result=await new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();xhr.open("POST","/api/videos");
        xhr.setRequestHeader("Content-Type",file.type);xhr.setRequestHeader("X-FS-CSRF",session.csrf);
        xhr.setRequestHeader("X-File-Name",encodeURIComponent(file.name));xhr.timeout=180000;
        xhr.upload.onprogress=e=>{if(owns(deskId,epoch) && e.lengthComputable)ui().notify(`正在上传 ${file.name} · ${Math.round(e.loaded/e.total*100)}%`);};
        xhr.onload=()=>{let data;try {data=JSON.parse(xhr.responseText);}catch{reject(new Error("上传响应无效"));return;}xhr.status===201 ? resolve(data) : reject(new Error(data.detail || "上传失败"));};
        xhr.onerror=()=>reject(new Error("上传连接中断，原调查已保留。"));xhr.ontimeout=xhr.onerror;xhr.send(file);
      });
      if (!owns(deskId,epoch)) return;
      const video=await api(`/api/videos/${result.id}`);if (!owns(deskId,epoch)) return;
      if (!await create(video)) return;
      ui().notify("上传完成，服务端正在预检素材。可在素材库查看进度。");
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
          ui().notify('暂时无法读取索引进度，正在重新连接；不会重新处理视频。');
          continue;
        }
        ui().notify(knowledgeWatch ? '无法读取索引进度。请重新打开该视频查看；已完成的处理会保留。' : error.message);
        return;
      }
      if (!active()) return;
      readFailures=0;
      try {
        const knowledge=video.knowledge_status;
        currentVideo=video;ui().updateKnowledge?.(video);
        if (knowledge?.required_for_chat && ["queued","indexing"].includes(knowledge.state)) {
          const label=`建立基础索引 · ${knowledge.completed_segments}/${knowledge.total_segments} 段`;
          $("video-state").textContent=label;ui().notify(label);continue;
        }
        ui().notify(`${labels[video.job?.state] || "处理素材"}${video.job?.total ? ` · ${video.job.completed}/${video.job.total} 窗口` : ""}`);
        if (["ready","failed","awaiting_configuration","cancelled"].includes(video.job?.state) && !needsWatch(video)) {
          await flush(); if (!active()) return;
          currentVideo=video; const saved=snapshot();suspended=true;
          try {ui().restore(saved,video);} finally {suspended=false;}
          syncSourceSwitcher();
          refreshChatBudget();
          changed();
          if (knowledge?.required_for_chat && knowledge.state === "ready") ui().notify("基础索引完成，可以开始提问。");
          else if (knowledge?.required_for_chat && knowledge.state === "waiting_budget") ui().notify("基础索引等待预算或授权；视频和已有回答仍可查看，已完成片段会保留。");
          else if (knowledge?.required_for_chat && ["failed","unknown","cancelled","waiting_processor"].includes(knowledge.state)) ui().notify("基础索引尚未完成；视频和已有回答仍可查看。");
          else if (video.job?.state === "failed") ui().notify(`预检或分析失败：${video.job.error}。素材保留，可重新处理或删除。`);
          else if (video.job?.state === "awaiting_configuration") ui().notify("预检完成，等待模型服务与预算配置。不会发送模型请求。");
          return;
        }
      }catch(error){if (active()) ui().notify(error.message);return;}
    }
  }
  async function recover({resumeMissing=false}={}) {
    if (!current || currentVideo.id === "demo_parcel") return;
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
        ui().notify(request.state === "unknown" ? "先前调用结果不确定，不会自动重复调用。原问题已保留。" : "先前查询仍在执行；点击恢复查询获取结果。");
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
          ui().notify('先前补查仍在处理；可以恢复查看或停止。原问题已保留。');
        }
        break;
      }
      if (supplement.state === 'unknown') {
        if (activeRequest?.desk===deskId && activeRequest.id===supplement.id) {
          activeRequest=null;$('resume-query').hidden=true;$('cancel-question').hidden=true;
        }
        if (wasActive || (draft && supplement.input?.question===draft))
          ui().notify('先前补查结果不确定，不会自动重发；原问题已保留。');
        continue;
      }
      if (supplement.state === 'done' && activeRequest?.desk===deskId && activeRequest.id===supplement.id) {
        activeRequest=null;$('resume-query').hidden=true;$('cancel-question').hidden=true;
      }
      if (['failed','cancelled','waiting_budget'].includes(supplement.state) &&
          activeRequest?.desk===deskId && activeRequest.id===supplement.id) {
        activeRequest=null;$('resume-query').hidden=true;$('cancel-question').hidden=true;
        ui().notify(supplement.state==='waiting_budget'
          ? '补查等待预算或授权；原问题已保留，不会自动重发。'
          : '先前补查未完成；原问题已保留，不会自动重发。');
        continue;
      }
      if (supplement.state === 'done' && !supplement.input) {
        if (wasActive) ui().notify('先前补查已完成，但缺少原问题记录；请检查已保留的草稿。');
        continue;
      }
      if (supplement.state === 'done' && supplement.input && !activeRequest) {
        const original=supplement.input;
        const prepared=await api('/api/auto-chat/prepare','POST',original);
        if (!valid()) return;
        if (prepared.kind!=='ready' || prepared.engine!=='index') {
          ui().notify(prepared.message || '补查已完成，但仍无法确认答案；原问题已保留。');
          continue;
        }
        const result=await api('/api/index-chat','POST',{...original,engine:'index'});
        if (!valid()) return;
        if (result?.response && !store().getSnapshot().records.some(r=>r.query.id===result.id)) {
          const parent=store().getSnapshot().records.find(r=>r.query.id===original.parent_request_id);
          ui().answer({...result,parentTurn:parent?.id || null},original.question);added=true;
          ui().notify('已恢复补查结果。');
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
    ui().notify('正在使用原请求恢复；不会创建新的请求。');
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
        ui().notify(outcome.suggested_action || '原请求仍在处理，请稍后恢复查看。');
        status(pending.state==='unknown' ? '调用结果不确定' : '正在查找证据…',pending.state==='unknown' ? 'error' : '');
        return;
      }
      if (outcome?.id!==pending.id || outcome.mode!=='real' || outcome.video_id!==pending.sourceId
          || outcome.response?.schema_version!=='0.1'
          || (outcome.source_generation!==undefined && outcome.source_generation!==pending.sourceGeneration))
        throw new Error('请求恢复响应无效');
      if (!isEmptyCancellation(outcome) && !isQueryResult(outcome,pending)) throw new Error('请求恢复响应无效');
      activeRequest=null;
      $('resume-query').hidden=true;$('cancel-question').hidden=true;
      if (!isEmptyCancellation(outcome) && !store().getSnapshot().records.some(r=>r.query.id===outcome.id)) {
        ui().answer({...outcome,parentTurn:pending.parentTurn},pending.body.question);
      }
      ui().notify(isEmptyCancellation(outcome) ? '原请求已取消，输入已保留。' : '已恢复原请求结果。');
      await flush();
    } catch(error) {
      if (valid()) {
        $('resume-query').hidden=false;
        ui().notify(`原请求恢复未完成：${error.message}。输入已保留；可再次恢复同一请求。`);
      }
    } finally {
      pending.recovering=false;
      if (owns(deskId,epoch) && submission===submissionEpoch && (!activeRequest || activeRequest===pending)) ui().busy(false);
    }
  }
  async function ask(question) {
    if (suspended || !current || !sourceReady(currentVideo)) return;
    if (activeRequest?.state!=="unknown" && activeRequest) {
      $('resume-query').hidden=false;ui().notify("查询仍在执行，请恢复当前请求，勿发起重复尝试。");return;
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
      ui().notify("已收到问题，正在检查…");
      await flush(); if (!owns(desk.id,epoch)) return;
      if (activeRequest) {await stopRequest();if (!owns(desk.id,epoch)) return;}
      let prepared, partialNotice='';
      if(engine==='auto') {
        prepared=await api('/api/auto-chat/prepare',"POST",body);
        if(!owns(desk.id,epoch)) return;
        if(!['index','local','cloud','model'].includes(prepared.engine)) throw new Error('自动处理方案无效，未发送问题');
        engine=prepared.engine;
        ui().notify(prepared.route_message || "已选择处理方式，正在检查…");
      }
      if (prepared?.has_index_matches && ['budget_blocked','supplement_unknown'].includes(prepared.kind)) {
        partialNotice=prepared.message+' 将显示已有索引结果。';
        prepared={...prepared,kind:'ready'};
      }
      if(prepared?.kind==='supplement_required') {
        pending={desk:desk.id,id:body.request_id,state:'running',kind:'supplement'};activeRequest=pending;
        $('cancel-question').hidden=false;
        ui().notify(Number.isSafeInteger(prepared.estimated_new_calls)
          ? `正在补查相关画面，最多 ${prepared.estimated_new_calls} 次云端请求…`
          : '正在补查相关画面…');
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
            ? '补查等待预算或授权；原问题已保留，不会自动重发。'
            : '补查未完成或结果不明；原问题已保留，不会自动重发。';
          if (!prepared.has_index_matches || !['waiting_budget','failed','unknown'].includes(supplement.state)) {
            ui().notify(message);status('补查未完成','error');return;
          }
          partialNotice='补查未完成或结果不明，不会自动重发；以下为已有索引结果。';
          prepared={...prepared,kind:'ready'};
        } else {
          activeRequest=null;pending=null;
          prepared=await api('/api/auto-chat/prepare','POST',body);
          if(!owns(desk.id,epoch))return;
          if(prepared.kind==='supplement_required') {
            prepared={...prepared,kind:'clarification_required',message:'已补查相关画面，但仍无法确认完整条件。原问题已保留，不会自动重复补查。'};
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
      status("正在查找证据…");
      let data;
      for (let i=0;i<15 || localMode || cloudMode;i++) {
        if(activeRequest!==pending)return;
        try {data=await api(endpoint,"POST",body);}catch(error){if(error.data?.kind==="query_control") {data=error.data;}else if(error.data?.response) {data=error.data;} else throw error;}
        if(activeRequest!==pending)return;
        if(data?.response) activeRequest=null;
        if(!owns(desk.id,epoch)) return;
        if(data.kind!=="query_control") break;
        if(localMode && data.suggested_action) ui().notify(data.suggested_action);
        if(data.state==="unknown") {pending.state="unknown";$('resume-query').hidden=false;ui().notify(data.suggested_action);status("调用结果不确定","error");return;}
        await new Promise(r=>setTimeout(r,Math.min(10000,3000+i*500)));
        if (!owns(desk.id,epoch) || activeRequest!==pending) return;
      }
      if(data?.response) {
        $('resume-query').hidden=true;
        if (!store().getSnapshot().records.some(r=>r.query.id===data.id)) ui().answer({...data,parentTurn},question);
        ui().notify(partialNotice);await flush();if (!owns(desk.id,epoch)) return;
      } else {$('resume-query').hidden=false;ui().notify("查询仍在执行，保留原问题。请从当前请求恢复，勿发起重复尝试。");}
    }catch(error){if(owns(desk.id,epoch) && submission===submissionEpoch){if(activeRequest===pending && pending?.kind==='supplement'){activeRequest=null;pending=null;}else if(activeRequest===pending && pending) $('resume-query').hidden=false;ui().notify(error.message === "context_has_no_verified_anchor"
      ? "所选问题没有有效证据，请点击「另起问题」并写出完整问题。输入已保留。"
      : `${error.message} · 原问题已保留。`);status("查找未完成","error");}}
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
        ui().notify("补查在停止前已完成，正在恢复结果…");
        try { await recover(); }
        catch { if (owns(desk,epoch)) ui().notify("补查已完成，但恢复失败；重新打开调查可再查看。输入已保留。"); }
        finally { if (owns(desk,epoch) && cancelledSubmission===submissionEpoch) ui().busy(false); }
        return;
      }
      ui().busy(false);
      ui().notify(completed && pending.kind!=='supplement' ? "查找已结束，已有结果和输入已保留。"
        : unknown ? "补查结果不确定，不会自动重发；输入已保留。"
        : failed ? "补查在停止前未完成；输入已保留，不会自动重发。"
        : "已停止本轮处理，输入已保留。可以修改后重新发送。");
      status(completed ? (pending.kind==='supplement' ? "补查已完成" : "查找已结束") : unknown ? "结果不确定" : failed ? "补查未完成" : "已停止处理");await flush();
    }catch(error){if(owns(desk,epoch) && operationSubmission===submissionEpoch)ui().notify("停止未完成："+error.message+"；输入已保留，可再次点击停止。");}
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
          ui().notify(cancelling?'已停止索引，已完成片段保留。':'正在建立基础索引；视频仍可播放。');
          if(!cancelling)watch(video.id,epoch);
        } catch(error) {if(owns(deskId,epoch))ui().notify(`${error.message} · 视频与记录已保留。`);}
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
      refreshSourceSwitcher().catch(error=>ui().notify(`视频列表读取失败：${error.message}。可在管理视频中重试。`));
      $("close-library").onclick=()=>$("workspace-library").close();
      $("delete-cancel").onclick=()=>$("delete-dialog").close();
    }catch(error){suspended=true;status("调查服务暂不可用","error");ui().notify(error.message);}
  }
  window.FrameSeekPersistence={flush,upload,ask,library,searchIndex:searchSavedFrames,canSearchOcr,openDemo:()=>openSource(demo).catch(e=>ui().notify(e.message)),
    newInvestigation:()=>create(currentVideo).catch(e=>ui().notify(e.message))};
  window.addEventListener("frameseek-catalog-ready",start,{once:true});
  if(ui()?.ready())start();
})();
