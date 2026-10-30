/* Portable reports preserve evidence provenance; text is always escaped in HTML. */
(function (root) {
  "use strict";
  const reviewNotes = answer => {
    const model = root.FrameSeekWorkbenchModel || (typeof module !== "undefined" && module.exports ? require("./workbench-model.js") : null);
    return model?.answerReviewNotes?.(answer) || [];
  };
  const clone = value => JSON.parse(JSON.stringify(value));
  const legacy = root.FrameSeekLegacyEnglish || (typeof module !== "undefined" && module.exports ? require("./legacy-demo-english.js") : null);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const time = value => {
    const milliseconds=Math.max(0,Math.round((Number(value)||0)*1000));
    const n=Math.floor(milliseconds/1000),fraction=milliseconds%1000;
    const whole=[Math.floor(n/3600),Math.floor(n/60)%60,n%60].map(i=>String(i).padStart(2,"0")).join(":");
    return fraction ? `${whole}.${String(fraction).padStart(3,"0")}` : whole;
  };
  const description = frame => [...new Set((frame.observations || [frame]).map(observation=>[observation.caption,observation.asr ? `Audio: ${observation.asr}` : null].filter(Boolean).join("\n")))].filter(Boolean).join("\n");
  function safeFrameUri(video, uri) {
    return typeof video === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(video) && typeof uri === "string"
      && new RegExp(`^/media/${video}/frames/[a-zA-Z0-9_.-]+\\.(?:jpg|jpeg|png|webp)$`).test(uri)
      && !uri.includes("..");
  }
  function create(state, createdAt = new Date().toISOString()) {
    const source = state.sourceInfo || {id:"demo_parcel",name:"Parcel movement · synthetic demo",metadata:{duration:18}};
    const kind = state.local ? "unindexed" : ["demo_parcel", "demo_basketball"].includes(source.id) ? "synthetic" : "real";
    const queries = new Map();
    for (const record of state.records || []) if(record.query && !record.hidden) queries.set(record.id,{...record.query,turn_id:record.id});
    for (const pin of state.pins || []) if(pin.query && !queries.has(pin.turnId)) queries.set(pin.turnId,{...pin.query,turn_id:pin.turnId,retained_by_finding:true});
    const frames = new Map();
    const questions=[];
    for (const original of queries.values()) {
      const query = legacy?.project(original) || original;
      const response=query.response;
      if (!response || !["ok","partial","no_result","error"].includes(response.status)) continue;
      for (const event of response.events || []) {
        if (event.video_id && event.video_id !== source.id) throw new Error("Cannot export evidence from another source.");
        for (const frame of event.evidence || []) {
          if(!safeFrameUri(source.id,frame.uri) || !Number.isFinite(frame.t)) throw new Error("Invalid evidence reference.");
          const key=`${frame.uri}|${frame.t}`;
          if(!frames.has(key)) frames.set(key,{id:`E${frames.size+1}`,video_id:source.id,...clone(frame),query_ids:[],turn_ids:[],observations:[]});
          const saved=frames.get(key);if(!saved.query_ids.includes(query.id))saved.query_ids.push(query.id);if(!saved.turn_ids.includes(query.turn_id))saved.turn_ids.push(query.turn_id);if(!saved.observations.some(o=>o.turn_id===query.turn_id && o.caption===frame.caption && o.asr===frame.asr))saved.observations.push({query_id:query.id,turn_id:query.turn_id,caption:frame.caption??null,asr:frame.asr??null});
        }
      }
      questions.push({id:query.id,turn_id:query.turn_id,parent_turn_id:query.parentTurn || null,
        question:query.question,mode:query.mode || "fixture",retained_by_finding:!!query.retained_by_finding,review_notes:reviewNotes(response.answer),response:clone(response)});
    }
    return {report_version:"1.0",created_at:createdAt,title:state.title?.trim() || "Untitled investigation",
      group_title:state.groupTitle?.trim() || "Key findings",note:state.note || "",
      source:{id:source.id,name:source.name,kind,duration_seconds:source.metadata?.duration ?? null,
        audio:source.metadata?.audio ?? null,expires_at:source.expires ?? null,capabilities:clone(source.capabilities || null)},
      scope:"Visible questions and sources of saved findings; this is not an exhaustive review of the video.",
      findings:(state.pins || []).map(pin=>({id:pin.id,kind:pin.kind,title:legacy?.display(pin.title,pin.query) || pin.title,
        query_id:pin.query.id,turn_id:pin.turnId,frame:pin.kind==="evidence" ? {...clone(pin.frame),caption:legacy?.display(pin.frame.caption,pin.query) ?? pin.frame.caption} : null})),
      questions,evidence:[...frames.values()],layout:clone(state.canvas || null)};
  }
  const provenance = report => report.source.kind === "synthetic"
    ? "Synthetic demo · Preset results · No model calls"
    : report.source.kind === "unindexed" ? "Local source · Not indexed · No analysis conclusions" : "Uploaded source · See each result status and unresolved item";
  function markdown(report) {
    const lines=[`# ${report.title}`,"",provenance(report),"",`Source: ${report.source.name || report.source.id}`,
      `Source ID: ${report.source.id}`,`Duration: ${report.source.duration_seconds == null ? "Unknown" : time(report.source.duration_seconds)}`,
      `Exported: ${report.created_at}`,"",report.scope,"",`## ${report.group_title}`,""];
    const lookup = new Map(report.questions.map(q=>[q.turn_id,q]));
    for(const finding of report.findings) {
      const q=lookup.get(finding.turn_id);if(!q)continue;
      if(finding.kind==="evidence") lines.push(`- ${time(finding.frame.t)} — ${description(finding.frame)}`,`  Source question: ${q.question}`);
      else {lines.push(`- ${q.response.answer}`,`  Source question: ${q.question} · ${q.response.status}`);for(const text of q.response.unresolved || [])lines.push(`  Unconfirmed: ${text}`);for(const text of q.review_notes || [])lines.push(`  Text review: ${text}`);}
    }
    if(!report.findings.length)lines.push("No saved findings.");
    lines.push("","## Investigation notes","",report.note || "No notes yet.","","## Questions and time evidence","");
    for(const [i,q] of report.questions.entries()) {
      lines.push(`### Q${i+1} ${q.question}`,"",`Result: ${q.response.status} · Turn: ${q.turn_id} · Request: ${q.id} · ${q.mode === "fixture" ? "Preset demo" : "Model analysis"}`,"",q.response.answer,"");
      for(const event of q.response.events || [])lines.push(`- ${time(event.start)}–${time(event.end)} · ${event.summary} · Support: ${event.support_level ?? "Measured score " + event.support_score}`);
      for(const text of q.review_notes || [])lines.push(`- Text review: ${text}`);
      for(const text of q.response.unresolved || [])lines.push(`- Unconfirmed: ${text}`);
      for(const error of q.response.errors || [])lines.push(`- Processing issue: ${error.code} · ${error.message}`);
      lines.push("");
    }
    lines.push("## Evidence index","");
    for(const frame of report.evidence)lines.push(`- ${frame.id} · ${time(frame.t)} · ${description(frame)}`,`  Frame: ${frame.frame_id} · Reference: ${frame.uri}`,`  Source requests: ${frame.query_ids.join(", ")}`);
    lines.push("","Media links require the original service and an active session. HTML exports may embed evidence available at download time.","");
    return lines.join("\n");
  }
  function html(report, embedded = {}) {
    const e=escape, lookup=new Map(report.questions.map(q=>[q.turn_id,q]));
    const evidence=report.evidence.map(frame=>{
      const url=embedded[frame.id];
      const image=typeof url==="string" && /^data:image\/(?:jpeg|png|webp);base64,[a-zA-Z0-9+/=]+$/.test(url)
        ? `<img src="${url}" alt="${e(description(frame))}">` : '<div class="missing">Frame not embedded · Original link requires an active session</div>';
      return `<figure>${image}<figcaption><b>${e(frame.id)} · ${time(frame.t)}</b><p>${e(description(frame))}</p><small>${e(frame.frame_id)}<br>${e(frame.uri)}<br>Source requests: ${e(frame.query_ids.join(", "))}</small></figcaption></figure>`;
    }).join("");
    const findings=report.findings.map(f=>{
      const q=lookup.get(f.turn_id);if(!q)return "";
      const text=f.kind==="evidence" ? `${time(f.frame.t)} · ${description(f.frame)}` : q.response.answer;
      return `<article><h3>${e(f.title)}</h3><p>${e(text)}</p><small>Source: ${e(q.question)} · ${e(q.id)}</small>${(q.review_notes || []).map(note=>`<p class="unknown">Text review: ${e(note)}</p>`).join("")}${f.kind==="conclusion" ? (q.response.unresolved || []).map(t=>`<p class="unknown">Unconfirmed: ${e(t)}</p>`).join("") : ""}</article>`;
    }).join("");
    const questions=report.questions.map((q,i)=>`<article><h3>Q${i+1} ${e(q.question)}</h3><small>${e(q.response.status)} · Turn ${e(q.turn_id)} · ${e(q.id)} · ${q.mode === "fixture" ? "Preset demo" : "Model analysis"}</small><p>${e(q.response.answer)}</p>${(q.review_notes || []).map(note=>`<p class="unknown">Text review: ${e(note)}</p>`).join("")}${(q.response.events || []).map(event=>`<p><b>${time(event.start)}–${time(event.end)}</b> ${e(event.summary)} <small>Support: ${e(event.support_level ?? event.support_score)}</small></p>`).join("")}${(q.response.unresolved || []).map(t=>`<p class="unknown">Unconfirmed: ${e(t)}</p>`).join("")}${(q.response.errors || []).map(t=>`<p class="unknown">Processing issue: ${e(t.code)} · ${e(t.message)}</p>`).join("")}</article>`).join("");
    const embeddedCount=report.evidence.filter(f=>embedded[f.id] && /^data:image\/(?:jpeg|png|webp);base64,[a-zA-Z0-9+/=]+$/.test(embedded[f.id])).length;
    return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>${e(report.title)} · FrameSeek</title><style>body{font:15px/1.75 system-ui,sans-serif;color:#18303c;background:#f4f6f7;max-width:1040px;margin:0 auto;padding:52px 32px}h1{font-size:34px;line-height:1.2}h2{margin-top:40px;border-bottom:1px solid #ced8dd;padding-bottom:10px}h3{font-size:17px}article,figure{background:white;border:1px solid #d7e0e4;border-radius:10px;padding:22px;margin:14px 0;break-inside:avoid}p{white-space:pre-wrap;overflow-wrap:anywhere}small,.meta{color:#58717d;overflow-wrap:anywhere}.unknown{border-left:3px solid #d16d30;padding-left:12px}.frames{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}figure{margin:0;padding:14px}img{width:100%;border-radius:5px}.missing{padding:30px;background:#edf1f3;color:#657781;font-size:13px}footer{margin-top:40px;color:#58717d;font-size:12px}@media print{body{background:white;padding:12mm;max-width:none;font-size:11pt}article,figure{box-shadow:none}h2{break-after:avoid}}@media(max-width:650px){.frames{grid-template-columns:1fr}}</style></head><body><p class="meta">FRAMESEEK / INVESTIGATION REPORT</p><h1>${e(report.title)}</h1><p>${e(provenance(report))}</p><p class="meta">Source: ${e(report.source.name || report.source.id)} · ${e(report.source.id)} · ${report.source.duration_seconds==null ? "Unknown duration" : time(report.source.duration_seconds)}<br>Exported: ${e(report.created_at)}</p><p>${e(report.scope)}</p><h2>${e(report.group_title)}</h2>${findings || '<p>No saved findings.</p>'}<h2>Investigation notes</h2><p>${e(report.note || "No notes yet.")}</p><h2>Questions and time evidence</h2>${questions || '<p>No completed questions.</p>'}<h2>Evidence frames</h2><p class="meta">${embeddedCount}/${report.evidence.length} embedded for offline viewing; remaining frames retain time and source references.</p><section class="frames">${evidence}</section><footer>Support levels describe evidence support, not accuracy. Partial results and uncertainties are retained. Original links expire with their source or session. This download may contain private material; store it securely. Print from your browser to create a PDF.</footer></body></html>`;
  }
  const api={create,markdown,html,time,safeFrameUri,provenance};
  if(typeof module!=="undefined" && module.exports)module.exports=api;
  else root.FrameSeekReport=api;
})(typeof window!=="undefined" ? window : globalThis);
