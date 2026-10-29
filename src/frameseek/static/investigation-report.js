/* Portable reports preserve evidence provenance; text is always escaped in HTML. */
(function (root) {
  "use strict";
  const reviewNotes = answer => {
    const model = root.FrameSeekWorkbenchModel || (typeof module !== "undefined" && module.exports ? require("./workbench-model.js") : null);
    return model?.answerReviewNotes?.(answer) || [];
  };
  const clone = value => JSON.parse(JSON.stringify(value));
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const time = value => {
    const milliseconds=Math.max(0,Math.round((Number(value)||0)*1000));
    const n=Math.floor(milliseconds/1000),fraction=milliseconds%1000;
    const whole=[Math.floor(n/3600),Math.floor(n/60)%60,n%60].map(i=>String(i).padStart(2,"0")).join(":");
    return fraction ? `${whole}.${String(fraction).padStart(3,"0")}` : whole;
  };
  const description = frame => [...new Set((frame.observations || [frame]).map(observation=>[observation.caption,observation.asr ? `原声：${observation.asr}` : null].filter(Boolean).join("\n")))].filter(Boolean).join("\n");
  function safeFrameUri(video, uri) {
    return typeof video === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(video) && typeof uri === "string"
      && new RegExp(`^/media/${video}/frames/[a-zA-Z0-9_.-]+\\.(?:jpg|jpeg|png|webp)$`).test(uri)
      && !uri.includes("..");
  }
  function create(state, createdAt = new Date().toISOString()) {
    const source = state.sourceInfo || {id:"demo_parcel",name:"包裹移动 · 合成演示",metadata:{duration:18}};
    const kind = state.local ? "unindexed" : source.id === "demo_parcel" ? "synthetic" : "real";
    const queries = new Map();
    for (const record of state.records || []) if(record.query && !record.hidden) queries.set(record.id,{...record.query,turn_id:record.id});
    for (const pin of state.pins || []) if(pin.query && !queries.has(pin.turnId)) queries.set(pin.turnId,{...pin.query,turn_id:pin.turnId,retained_by_finding:true});
    const frames = new Map();
    const questions=[];
    for (const query of queries.values()) {
      const response=query.response;
      if (!response || !["ok","partial","no_result","error"].includes(response.status)) continue;
      for (const event of response.events || []) {
        if (event.video_id && event.video_id !== source.id) throw new Error("报告包含其他素材的证据，无法导出。");
        for (const frame of event.evidence || []) {
          if(!safeFrameUri(source.id,frame.uri) || !Number.isFinite(frame.t)) throw new Error("报告证据引用无效。");
          const key=`${frame.uri}|${frame.t}`;
          if(!frames.has(key)) frames.set(key,{id:`E${frames.size+1}`,video_id:source.id,...clone(frame),query_ids:[],turn_ids:[],observations:[]});
          const saved=frames.get(key);if(!saved.query_ids.includes(query.id))saved.query_ids.push(query.id);if(!saved.turn_ids.includes(query.turn_id))saved.turn_ids.push(query.turn_id);if(!saved.observations.some(o=>o.turn_id===query.turn_id && o.caption===frame.caption && o.asr===frame.asr))saved.observations.push({query_id:query.id,turn_id:query.turn_id,caption:frame.caption??null,asr:frame.asr??null});
        }
      }
      questions.push({id:query.id,turn_id:query.turn_id,parent_turn_id:query.parentTurn || null,
        question:query.question,mode:query.mode || "fixture",retained_by_finding:!!query.retained_by_finding,review_notes:reviewNotes(response.answer),response:clone(response)});
    }
    return {report_version:"1.0",created_at:createdAt,title:state.title?.trim() || "未命名调查",
      group_title:state.groupTitle?.trim() || "关键发现",note:state.note || "",
      source:{id:source.id,name:source.name,kind,duration_seconds:source.metadata?.duration ?? null,
        audio:source.metadata?.audio ?? null,expires_at:source.expires ?? null,capabilities:clone(source.capabilities || null)},
      scope:"最近12轮问题及已保存发现的来源；不代表完整历史或全视频穷尽检查。",
      findings:(state.pins || []).map(pin=>({id:pin.id,kind:pin.kind,title:pin.title,
        query_id:pin.query.id,turn_id:pin.turnId,frame:pin.kind==="evidence" ? clone(pin.frame) : null})),
      questions,evidence:[...frames.values()],layout:clone(state.canvas || null)};
  }
  const provenance = report => report.source.kind === "synthetic"
    ? "合成演示 · 预设结果 · 未调用模型"
    : report.source.kind === "unindexed" ? "本地素材 · 尚未完成索引 · 不作分析结论" : "真实素材 · 证据结论以各轮状态及未确认项为准";
  function markdown(report) {
    const lines=[`# ${report.title}`,"",provenance(report),"",`素材：${report.source.name || report.source.id}`,
      `素材 ID：${report.source.id}`,`时长：${report.source.duration_seconds == null ? "未知" : time(report.source.duration_seconds)}`,
      `导出：${report.created_at}`,"",report.scope,"",`## ${report.group_title}`,""];
    const lookup = new Map(report.questions.map(q=>[q.turn_id,q]));
    for(const finding of report.findings) {
      const q=lookup.get(finding.turn_id);if(!q)continue;
      if(finding.kind==="evidence") lines.push(`- ${time(finding.frame.t)} — ${description(finding.frame)}`,`  来源问题：${q.question}`);
      else {lines.push(`- ${q.response.answer}`,`  来源问题：${q.question} · ${q.response.status}`);for(const text of q.response.unresolved || [])lines.push(`  未确认：${text}`);for(const text of q.review_notes || [])lines.push(`  文本复核：${text}`);}
    }
    if(!report.findings.length)lines.push("尚未保存发现。");
    lines.push("","## 调查笔记","",report.note || "尚未添加笔记。","","## 问题与时间证据","");
    for(const [i,q] of report.questions.entries()) {
      lines.push(`### Q${i+1} ${q.question}`,"",`结果：${q.response.status} · 轮次：${q.turn_id} · 请求：${q.id} · ${q.mode === "fixture" ? "预设演示" : "真实分析"}`,"",q.response.answer,"");
      for(const event of q.response.events || [])lines.push(`- ${time(event.start)}–${time(event.end)} · ${event.summary} · 支持：${event.support_level ?? "已测量分数 " + event.support_score}`);
      for(const text of q.review_notes || [])lines.push(`- 文本复核：${text}`);
      for(const text of q.response.unresolved || [])lines.push(`- 未确认：${text}`);
      for(const error of q.response.errors || [])lines.push(`- 处理问题：${error.code} · ${error.message}`);
      lines.push("");
    }
    lines.push("## 证据索引","");
    for(const frame of report.evidence)lines.push(`- ${frame.id} · ${time(frame.t)} · ${description(frame)}`,`  帧：${frame.frame_id} · 引用：${frame.uri}`,`  来源请求：${frame.query_ids.join(", ")}`);
    lines.push("","媒体引用需要原服务和有效会话；过期后不保证可访问。HTML导出可嵌入下载时可用的证据画面。","");
    return lines.join("\n");
  }
  function html(report, embedded = {}) {
    const e=escape, lookup=new Map(report.questions.map(q=>[q.turn_id,q]));
    const evidence=report.evidence.map(frame=>{
      const url=embedded[frame.id];
      const image=typeof url==="string" && /^data:image\/(?:jpeg|png|webp);base64,[a-zA-Z0-9+/=]+$/.test(url)
        ? `<img src="${url}" alt="${e(description(frame))}">` : '<div class="missing">未嵌入画面 · 原引用需有效会话</div>';
      return `<figure>${image}<figcaption><b>${e(frame.id)} · ${time(frame.t)}</b><p>${e(description(frame))}</p><small>${e(frame.frame_id)}<br>${e(frame.uri)}<br>来源请求：${e(frame.query_ids.join(", "))}</small></figcaption></figure>`;
    }).join("");
    const findings=report.findings.map(f=>{
      const q=lookup.get(f.turn_id);if(!q)return "";
      const text=f.kind==="evidence" ? `${time(f.frame.t)} · ${description(f.frame)}` : q.response.answer;
      return `<article><h3>${e(f.title)}</h3><p>${e(text)}</p><small>来源：${e(q.question)} · ${e(q.id)}</small>${(q.review_notes || []).map(note=>`<p class="unknown">文本复核：${e(note)}</p>`).join("")}${f.kind==="conclusion" ? (q.response.unresolved || []).map(t=>`<p class="unknown">未确认：${e(t)}</p>`).join("") : ""}</article>`;
    }).join("");
    const questions=report.questions.map((q,i)=>`<article><h3>Q${i+1} ${e(q.question)}</h3><small>${e(q.response.status)} · 轮次 ${e(q.turn_id)} · ${e(q.id)} · ${q.mode === "fixture" ? "预设演示" : "真实分析"}</small><p>${e(q.response.answer)}</p>${(q.review_notes || []).map(note=>`<p class="unknown">文本复核：${e(note)}</p>`).join("")}${(q.response.events || []).map(event=>`<p><b>${time(event.start)}–${time(event.end)}</b> ${e(event.summary)} <small>支持：${e(event.support_level ?? event.support_score)}</small></p>`).join("")}${(q.response.unresolved || []).map(t=>`<p class="unknown">未确认：${e(t)}</p>`).join("")}${(q.response.errors || []).map(t=>`<p class="unknown">处理问题：${e(t.code)} · ${e(t.message)}</p>`).join("")}</article>`).join("");
    const embeddedCount=report.evidence.filter(f=>embedded[f.id] && /^data:image\/(?:jpeg|png|webp);base64,[a-zA-Z0-9+/=]+$/.test(embedded[f.id])).length;
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>${e(report.title)} · FrameSeek</title><style>body{font:15px/1.75 system-ui,sans-serif;color:#18303c;background:#f4f6f7;max-width:1040px;margin:0 auto;padding:52px 32px}h1{font-size:34px;line-height:1.2}h2{margin-top:40px;border-bottom:1px solid #ced8dd;padding-bottom:10px}h3{font-size:17px}article,figure{background:white;border:1px solid #d7e0e4;border-radius:10px;padding:22px;margin:14px 0;break-inside:avoid}p{white-space:pre-wrap;overflow-wrap:anywhere}small,.meta{color:#58717d;overflow-wrap:anywhere}.unknown{border-left:3px solid #d16d30;padding-left:12px}.frames{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}figure{margin:0;padding:14px}img{width:100%;border-radius:5px}.missing{padding:30px;background:#edf1f3;color:#657781;font-size:13px}footer{margin-top:40px;color:#58717d;font-size:12px}@media print{body{background:white;padding:12mm;max-width:none;font-size:11pt}article,figure{box-shadow:none}h2{break-after:avoid}}@media(max-width:650px){.frames{grid-template-columns:1fr}}</style></head><body><p class="meta">FRAMESEEK / INVESTIGATION REPORT</p><h1>${e(report.title)}</h1><p>${e(provenance(report))}</p><p class="meta">素材：${e(report.source.name || report.source.id)} · ${e(report.source.id)} · ${report.source.duration_seconds==null ? "未知时长" : time(report.source.duration_seconds)}<br>导出：${e(report.created_at)}</p><p>${e(report.scope)}</p><h2>${e(report.group_title)}</h2>${findings || '<p>尚未保存发现。</p>'}<h2>调查笔记</h2><p>${e(report.note || "尚未添加笔记。")}</p><h2>问题与时间证据</h2>${questions || '<p>尚无已完成问题。</p>'}<h2>证据画面</h2><p class="meta">${embeddedCount}/${report.evidence.length} 张已嵌入，可离线查看；其余保留时间与来源引用。</p><section class="frames">${evidence}</section><footer>支持等级表示证据支持程度，并非正确率。部分结果与未确认项均已保留。原服务引用会随素材/会话到期失效；此下载副本可能包含私有素材，请妥善保存。可在浏览器打印为 PDF。</footer></body></html>`;
  }
  const api={create,markdown,html,time,safeFrameUri,provenance};
  if(typeof module!=="undefined" && module.exports)module.exports=api;
  else root.FrameSeekReport=api;
})(typeof window!=="undefined" ? window : globalThis);
