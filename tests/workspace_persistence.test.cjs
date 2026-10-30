/* Deterministic deferred-network regressions for durable workspace ownership. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { createStore } = require("../src/frameseek/static/workbench-model.js");
const tick = () => new Promise(setImmediate);
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const answer = (id, video = "video-A") => ({
  id, question: `Question ${id}`, mode: "real", video_id: video,
  response: { schema_version: "0.1", status: "ok", answer: id, events: [], unresolved: [], errors: [], followups: [], trace: { visible: { segments_reviewed: 1 } } },
});
class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.value = ""; this.hidden = false;
    this.attributes = {}; this.listeners = {}; this.style = {}; this.classes = new Set();
    this.firstChild = { textContent: "" };
    this.classList = { add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x), toggle: (x, on) => on ? this.classes.add(x) : this.classes.delete(x) };
  }
  set className(value) { this.classes = new Set(value.split(" ")); }
  get className() { return [...this.classes].join(" "); }
  append(...nodes) { nodes.forEach(n => { n.parent = this; this.children.push(n); }); }
  insertBefore(node, target) { node.parent=this; this.children.splice(this.children.indexOf(target),0,node); }
  prepend(node) { node.parent = this; this.children.unshift(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  remove() { this.parent.children = this.parent.children.filter(n => n !== this); }
  get firstElementChild() { return this.children[0]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { (this.listeners[key] ||= []).push(fn); }
  dispatchEvent(event) { (this.listeners[event.type] || []).forEach(fn => fn(event)); }
  close() { this.open = false; }
  showModal() { this.open = true; }
  focus() {}
  load() {}
  pause() { this.paused = true; }
  scrollIntoView() {}
  querySelector() { return new Element(); }
}
async function harness({ state = {}, requests = [], job = "ready", realReady = true, storage = new Map(), owner = "owner-A", revision = 1, queryReferences = false, startupGate = null, awaitStartup = true, readingSuggestionsVisible = true } = {}) {
  const store = createStore(), ids = new Map(), calls = [], answers = [], timers = new Map();
  let seq = 100, timerSeq = 0, ready = false, busy = false, context = null, override = null, focusedSelector = null;
  const get = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
  const desks = new Map([
    ["desk-A", { id: "desk-A", video: "video-A", title: "A", revision, state }],
    ["desk-B", { id: "desk-B", video: "video-B", title: "B", revision: 1, state: {} }],
  ]);
  const videos = new Map([
    ["video-A", { id: "video-A", name: "A.mp4", metadata: { windows: 1 }, bytes: 10, job: { state: job } }],
    ["video-B", { id: "video-B", name: "B.mp4", metadata: { windows: 1 }, bytes: 10, job: { state: "ready" } }],
  ]);
  const requestLists = new Map([["desk-A", requests], ["desk-B", []]]);
  const ui = {
    ready: () => ready, busy: value => { busy = value; }, notify: value => { get("notice").textContent = value; },
    preflightFeedback: value => { get("feedback").textContent = value; },
    snapshot: () => ({ ...store.getSnapshot(), ui: { draft: get("question").value } }),
    currentContext: () => context, showSource() {},
    restore(saved, video) { store.hydrate({ ...saved, sourceInfo: video, local: video.job?.state !== "ready" }); get("question").value = saved.ui?.draft || ""; },
    applySource(video) { store.clear(); store.describe(video); store.source(video.job?.state !== "ready"); get("question").value = ""; },
    restoreDraft: value => { get("question").value = value; },
    answer(query, submittedDraft) {
      answers.push(query); store.record(`task-${++seq}`, query);
      if (submittedDraft !== undefined && get("question").value.trim() === submittedDraft) get("question").value = "";
    },
  };
  const fetch = async (url, options = {}) => {
    const call = { url, method: options.method || "GET", body: options.body && JSON.parse(options.body) };
    calls.push(call);
    const special = override?.(call);
    if (special !== undefined) return special;
    if (url === "/api/session") {
      if (startupGate) await startupGate.promise;
      return response({ csrf: "csrf", owner_id: owner, ttl_hours: 24, real_ready: realReady, query_reference_snapshots: queryReferences });
    }
    if (url === "/api/investigations" && call.method === "GET") return response({ items: [...desks.values()] });
    if (url === "/api/investigations" && call.method === "POST") {
      const desk = { id: "new-demo", video: call.body.video_id, title: call.body.title, revision: 1, state: {} };
      desks.set(desk.id, desk); return response(desk, 201);
    }
    if (url === "/api/videos") return response({ items: [...videos.values()] });
    const video = url.match(/^\/api\/videos\/([^/]+)$/);
    if (video) return response(videos.get(video[1]));
    const req = url.match(/^\/api\/investigations\/([^/]+)\/requests$/);
    if (req) return response({ items: requestLists.get(req[1]) || [] });
    const deskMatch = url.match(/^\/api\/investigations\/([^/]+)$/);
    if (deskMatch) {
      const desk = desks.get(deskMatch[1]);
      if (call.method === "DELETE") { desks.delete(desk.id); return response(null, 204); }
      if (call.method === "PUT") { desk.state = call.body.state; return response({ revision: ++desk.revision }); }
      return response({ ...desk });
    }
    if (url === "/api/chat/prepare") return response({ kind: "ready" });
    throw new Error(`Unexpected request: ${call.method} ${url}`);
  };
  const windowEvents = {};
  const window = { FrameSeekSessionUI: ui, FrameSeekWorkbench: store,
    requestAnimationFrame(fn) { fn(); },
    addEventListener(type,fn) { (windowEvents[type] ||= []).push(fn); },
    dispatchEvent(event) { (windowEvents[event.type] || []).forEach(fn=>fn(event)); } };
  const body = new Element(); body.setAttribute("data-workspace-startup", "loading");
  const sandbox = vm.createContext({ Event: class { constructor(type) { this.type=type; } }, window, document: { body, getElementById: get, createElement: () => new Element(), addEventListener() {}, querySelector: selector => ({getClientRects() { return selector.startsWith('.reading-demo') && !readingSuggestionsVisible ? [] : [{}]; }, focus() { focusedSelector=selector; }}) }, fetch,
    sessionStorage: {getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    localStorage: { getItem: () => "desk-A", setItem() {} }, crypto: { randomUUID: () => `request-${++seq}` },
    setTimeout: (fn, delay) => { const id = ++timerSeq; timers.set(id, { fn, delay }); return id; }, clearTimeout: id => timers.delete(id),
  });
  // Expose closure entry points in the VM only; production source is unchanged.
  const source = fs.readFileSync(require.resolve("../src/frameseek/static/workspace.js"), "utf8")
    .replace("window.FrameSeekPersistence={flush,", "window.testWorkspace={start,openDesk,openSource,refreshSourceSwitcher,recover,confirmDelete,watch,get:()=>({current,currentVideo,activeRequest,requestEpoch})}; window.FrameSeekPersistence={flush,");
  vm.runInContext(source, sandbox);
  get("chat-engine").value="model";
  ready = true; const startup = window.testWorkspace.start(); if (awaitStartup) await startup;
  return { store, get, calls, answers, desks, videos, requestLists, ui, window, timers, body, startup,
    get busy() { return busy; }, get focusedSelector() { return focusedSelector; }, setContext(value) { context = value; }, setOverride(fn) { override = fn; },
    async timer(delay) { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry, `Missing ${delay}ms timer`); timers.delete(entry[0]); entry[1].fn(); await tick(); },
  };
}

test("starting the basketball demo focuses a suggested question", async () => {
  const h = await harness();
  await h.window.FrameSeekPersistence.openDemo();
  assert.equal(h.window.testWorkspace.get().currentVideo.id, "demo_basketball");
  assert.equal(h.focusedSelector, ".reading-demo-examples .suggestion-buttons button");
});

test("starting the demo from canvas focuses its visible suggestion", async () => {
  const h = await harness({readingSuggestionsVisible: false});
  await h.window.FrameSeekPersistence.openDemo();
  assert.equal(h.focusedSelector, "#suggestions button");
});

test("startup keeps the fixture hidden until the remembered investigation is restored", async () => {
  const gate = deferred();
  const h = await harness({ startupGate: gate, awaitStartup: false });
  assert.equal(h.body.getAttribute("data-workspace-startup"), "loading");
  gate.resolve(); await h.startup;
  assert.equal(h.body.getAttribute("data-workspace-startup"), null);
  assert.equal(h.window.testWorkspace.get().currentVideo.id, "video-A");
});

test("startup failures show a retry state instead of the fixture", async () => {
  const gate = deferred();
  const h = await harness({ startupGate: gate, awaitStartup: false });
  gate.reject(new Error("offline")); await h.startup;
  assert.equal(h.body.getAttribute("data-workspace-startup"), "error");
});

test("a fresh completed question clears recovery left by a prior unknown request", async () => {
  const h = await harness({ requests: [{ id: "old-unknown", state: "unknown", input: { question: "old question" } }] });
  assert.equal(h.get("resume-query").hidden, false);
  h.get("question").value = "new question";
  h.setOverride(c => c.url.endsWith("/cancel") ? response({})
    : c.url === "/api/chat" ? response(answer(c.body.request_id)) : undefined);
  await h.window.FrameSeekPersistence.ask("new question");
  assert.equal(h.get("resume-query").hidden, true);
  assert.equal(h.window.testWorkspace.get().activeRequest, null);
  assert.equal(h.calls.filter(c => c.url === "/api/chat").length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith("/cancel")).length, 1);
});

test("an explicit recovery resubmits only the same missing request and preserves a newer draft", async () => {
  const h=await harness(); let posts=0;
  h.setOverride(c=>{
    if(c.url==='/api/chat') {
      if(++posts===1) throw new Error('connection_failed');
      return response(answer(c.body.request_id));
    }
  });
  h.get('question').value='original question';
  await h.window.FrameSeekPersistence.ask('original question');
  await h.window.testWorkspace.recover();
  assert.equal(posts,1,'automatic recovery must not resend the request');
  assert.equal(h.get('resume-query').hidden,false);
  h.get('question').value='newer draft';
  await h.get('resume-query').onclick();
  const requests=h.calls.filter(c=>c.url==='/api/chat');
  assert.equal(requests.length,2);
  assert.deepEqual(requests[1].body,requests[0].body);
  assert.equal(h.answers.length,1);
  assert.equal(h.answers[0].id,requests[0].body.request_id);
  assert.equal(h.get('question').value,'newer draft');
  assert.equal(h.window.testWorkspace.get().activeRequest,null);
});

test('repeated recovery clicks do not submit the same request concurrently',async()=>{
  const h=await harness(), pending=deferred();let posts=0;
  h.setOverride(c=>c.url==='/api/chat' ? (++posts===1 ? Promise.reject(new Error('connection_failed')) : pending.promise) : undefined);
  await h.window.FrameSeekPersistence.ask('original question');
  const first=h.get('resume-query').onclick();await tick();
  await h.get('resume-query').onclick();
  assert.equal(posts,2);
  const request=h.calls.find(c=>c.url==='/api/chat');
  pending.resolve(response(answer(request.body.request_id)));await first;
  assert.equal(h.answers.length,1);
});

test('explicit recovery never resends a missing supplement',async()=>{
  const h=await harness();let listed=true;
  const input={investigation_id:'desk-A',request_id:'supp-1',generation:0,question:'original question'};
  h.setOverride(c=>c.url==='/api/investigations/desk-A/requests'
    ? response({items:[],supplements:listed ? [{id:'supp-1',state:'running',input}] : []}) : undefined);
  await h.window.testWorkspace.recover();listed=false;
  await h.get('resume-query').onclick();
  assert.equal(h.calls.some(c=>c.url==='/api/knowledge-chat/supplement'),false);
});

for(const firstToFinish of ['original','recovery']) {
  test(`same-ID recovery and original response are displayed once when ${firstToFinish} finishes first`, async()=>{
    const h=await harness(), original=deferred(), recovered=deferred(); let posts=0;
    h.setOverride(c=>c.url==='/api/chat' ? (++posts===1 ? original.promise : recovered.promise) : undefined);
    h.get('question').value='original question';
    const asking=h.window.FrameSeekPersistence.ask('original question');await tick();
    const recovering=h.get('resume-query').onclick();await tick();
    const requests=h.calls.filter(c=>c.url==='/api/chat');
    assert.equal(requests.length,2);assert.deepEqual(requests[1].body,requests[0].body);
    const result=answer(requests[0].body.request_id);
    h.get('question').value='newer draft';
    if(firstToFinish==='original') {original.resolve(response(result));await asking;recovered.resolve(response(result));await recovering;}
    else {recovered.resolve(response(result));await recovering;original.resolve(response(result));await asking;}
    assert.equal(h.answers.length,1);assert.equal(h.get('question').value,'newer draft');
  });
}

for(const knownBy of ['response','recovery']) {
  test(`explicit recovery never resends an unknown request reported by ${knownBy}`, async()=>{
    const h=await harness();let posts=0, requestId;
    h.setOverride(c=>{
      if(c.url==='/api/chat') {
        posts++;requestId=c.body.request_id;
        if(knownBy==='response') return response({kind:'query_control',state:'unknown',request_id:requestId},409);
        throw new Error('connection_failed');
      }
      if(c.url==='/api/investigations/desk-A/requests' && knownBy==='recovery')
        return response({items:[{id:requestId,state:'unknown',input:{question:'original question'}}]});
    });
    h.get('question').value='original question';
    await h.window.FrameSeekPersistence.ask('original question');
    await h.get('resume-query').onclick();
    assert.equal(posts,1);
    assert.equal(h.window.testWorkspace.get().activeRequest.state,'unknown');
    assert.equal(h.get('question').value,'original question');
  });
}

test('same-ID recovery cannot publish after navigating to another investigation',async()=>{
  const h=await harness(), recovered=deferred();let posts=0, requestId;
  h.desks.get('desk-B').state.ui={draft:'desk B draft'};
  h.setOverride(c=>{
    if(c.url==='/api/chat') {
      requestId=c.body.request_id;
      if(++posts===1) throw new Error('connection_failed');
      return recovered.promise;
    }
    if(c.url.endsWith('/cancel')) {
      const cancelled=answer(requestId);cancelled.response.status='error';
      cancelled.response.errors=[{code:'query_cancelled'}];return response(cancelled);
    }
  });
  await h.window.FrameSeekPersistence.ask('original question');
  const pending=h.get('resume-query').onclick();await tick();
  await h.window.testWorkspace.openDesk('desk-B');
  recovered.resolve(response(answer(requestId)));await pending;
  assert.equal(h.window.testWorkspace.get().current.id,'desk-B');
  assert.equal(h.get('question').value,'desk B draft');assert.equal(h.answers.length,0);
});

test('a late recovery cannot release a newer in-flight question',async()=>{
  const h=await harness(), original=deferred(), recovered=deferred(), newer=deferred();let posts=0;
  h.setOverride(c=>c.url==='/api/chat' ? [original,recovered,newer][posts++].promise : undefined);
  const asking=h.window.FrameSeekPersistence.ask('original question');await tick();
  const recovering=h.get('resume-query').onclick();await tick();
  const requestId=h.calls.find(c=>c.url==='/api/chat').body.request_id;
  original.resolve(response(answer(requestId)));await asking;
  const next=h.window.FrameSeekPersistence.ask('new question');await tick();
  assert.equal(h.busy,true);
  recovered.resolve(response(answer(requestId)));await recovering;
  assert.equal(h.busy,true);assert.equal(h.answers.length,1);
  const nextId=h.calls.filter(c=>c.url==='/api/chat')[2].body.request_id;
  newer.resolve(response(answer(nextId)));await next;
  assert.equal(h.answers.length,2);
});

test('same-ID recovery cannot publish after the source generation changes',async()=>{
  const h=await harness(), recovered=deferred();let posts=0, requestId;
  h.videos.get('video-A').generation=2;
  h.setOverride(c=>{
    if(c.url==='/api/chat') {
      requestId=c.body.request_id;
      if(++posts===1) throw new Error('connection_failed');
      return recovered.promise;
    }
  });
  await h.window.FrameSeekPersistence.ask('original question');
  const pending=h.get('resume-query').onclick();await tick();
  h.window.testWorkspace.get().currentVideo.generation=3;
  recovered.resolve(response(answer(requestId)));await pending;
  assert.equal(h.answers.length,0);
});

test('same-ID recovery cannot publish after the investigation is reset',async()=>{
  const h=await harness(), recovered=deferred();let posts=0, requestId;
  h.setOverride(c=>{
    if(c.url==='/api/chat') {
      requestId=c.body.request_id;
      if(++posts===1) throw new Error('connection_failed');
      return recovered.promise;
    }
  });
  await h.window.FrameSeekPersistence.ask('original question');
  const pending=h.get('resume-query').onclick();await tick();
  h.store.clear();h.get('question').value='draft after reset';
  recovered.resolve(response(answer(requestId)));await pending;
  assert.equal(h.answers.length,0);assert.equal(h.get('question').value,'draft after reset');
});

test("failed deletion retains the active investigation and subsequent autosave", async () => {
  const h = await harness();
  h.setOverride(c => c.method === "DELETE" ? response({ detail: "delete_failed" }, 500) : undefined);
  h.window.testWorkspace.confirmDelete(h.desks.get("desk-A"), "investigations");
  await h.get("delete-confirm").onclick();
  assert.equal(h.window.testWorkspace.get().current.id, "desk-A");
  h.store.annotate("saved after failed deletion"); await h.window.FrameSeekPersistence.flush();
  assert.equal(h.desks.get("desk-A").state.note, "saved after failed deletion");
  assert.equal(h.busy, false);
});

test('budget and capability preflight preserve the draft without a query or failure card', async()=>{
 const h=await harness();
 for(const kind of ['budget_blocked','capability_blocked','clarification_required']) {
  h.setOverride(c=>c.url==='/api/chat/prepare' ? response({kind,message:'尚未调用模型，输入已保留。'}) : undefined);
  h.get('question').value='我的原问题';
  await h.window.FrameSeekPersistence.ask('我的原问题');
  assert.equal(h.get('question').value,'我的原问题');
  assert.equal(h.answers.length,0);assert.equal(h.calls.filter(c=>c.url==='/api/chat').length,0);
  assert.equal(h.window.testWorkspace.get().activeRequest,null);
  assert.match(h.get('notice').textContent,/尚未调用模型/);
  assert.match(h.get('feedback').textContent,/尚未调用模型/);
  assert.equal(h.busy,false,'send is released after preflight rejection');
  h.get('question').dispatchEvent({type:'input'});
  assert.equal(h.get('notice').textContent,'','editing clears a stale preflight explanation');
 }
});

test('a late budget snapshot cannot replace the current desk budget or draft', async()=>{
 const h=await harness(),old=deferred();let reads=0;
 h.setOverride(c=>c.url==='/api/runtime/status' ? (++reads===1 ? old.promise : response({
  dispatch_prerequisites_met:false,budget:{remaining_calls:0,call_cap:40,short_window_calls:1}
 })) : undefined);
 await h.window.testWorkspace.openDesk('desk-A');
 await h.window.testWorkspace.openDesk('desk-B');await tick();
 h.get('question').value='B的草稿';
 assert.match(h.get('chat-budget-status').textContent,/exhausted 40/);
 old.resolve(response({dispatch_prerequisites_met:true,budget:{remaining_calls:1,call_cap:40,short_window_calls:1}}));
 await tick();
 assert.match(h.get('chat-budget-status').textContent,/exhausted 40/);
 assert.equal(h.get('question').value,'B的草稿');
});

test("navigation drains note and draft edits made while the first save is pending", async () => {
  const h = await harness(), firstSave = deferred(); let saves = 0;
  h.store.annotate("before save"); h.get("question").value = "before draft";
  h.setOverride(c => c.url === "/api/investigations/desk-A" && c.method === "PUT" && ++saves === 1 ? firstSave.promise : undefined);
  const navigation = h.window.testWorkspace.openDesk("desk-B"); await tick();
  h.store.annotate("latest note"); h.get("question").value = "latest draft";
  firstSave.resolve(response({ revision: 2 })); await navigation;
  const writes = h.calls.filter(c => c.url === "/api/investigations/desk-A" && c.method === "PUT");
  assert.equal(writes.length, 2); assert.equal(writes[1].body.revision, 2);
  assert.equal(h.desks.get("desk-A").state.note, "latest note");
  assert.equal(h.desks.get("desk-A").state.ui.draft, "latest draft");
  const lastWrite = h.calls.indexOf(writes[1]);
  const restoreFetch = h.calls.findIndex(c => c.url === "/api/investigations/desk-B" && c.method === "GET");
  assert.ok(lastWrite < restoreFetch); assert.equal(h.window.testWorkspace.get().current.id, "desk-B");
});

test("a failed draining save keeps the edited desk active and preserves its CAS error", async () => {
  const h = await harness(), firstSave = deferred(); let saves = 0;
  h.store.annotate("before save");
  h.setOverride(c => {
    if (c.url !== "/api/investigations/desk-A" || c.method !== "PUT") return;
    return ++saves === 1 ? firstSave.promise : response({ detail: "revision_conflict" }, 409);
  });
  const navigation = h.window.testWorkspace.openDesk("desk-B"); await tick();
  h.store.annotate("latest note"); h.get("question").value = "latest draft";
  firstSave.resolve(response({ revision: 2 }));
  await assert.rejects(navigation, /revision_conflict/);
  assert.equal(h.window.testWorkspace.get().current.id, "desk-A");
  assert.equal(h.store.getSnapshot().note, "latest note"); assert.equal(h.get("question").value, "latest draft");
  assert.equal(h.get("save-state").dataset.state, "error");
});

test("a clean navigation still saves edits made during its target desk GET", async () => {
  const h = await harness(), target = deferred();
  h.setOverride(c => c.url === "/api/investigations/desk-B" && c.method === "GET" ? target.promise : undefined);
  const navigation = h.window.testWorkspace.openDesk("desk-B"); await tick();
  h.store.rename("Latest title"); h.store.annotate("Latest note"); h.get("question").value = "Latest draft";
  target.resolve(response(h.desks.get("desk-B"))); await navigation;
  assert.equal(h.desks.get("desk-A").state.title, "Latest title");
  assert.equal(h.desks.get("desk-A").state.note, "Latest note");
  assert.equal(h.desks.get("desk-A").state.ui.draft, "Latest draft");
  assert.equal(h.window.testWorkspace.get().current.id, "desk-B");
});

test("target video GET remains within the old desk's saving boundary", async () => {
  const h = await harness(), video = deferred();
  h.setOverride(c => c.url === "/api/videos/video-B" ? video.promise : undefined);
  const navigation = h.window.testWorkspace.openDesk("desk-B"); await tick();
  h.store.annotate("Edited during video fetch"); h.get("question").value = "Video fetch draft";
  video.resolve(response(h.videos.get("video-B"))); await navigation;
  assert.equal(h.desks.get("desk-A").state.note, "Edited during video fetch");
  assert.equal(h.desks.get("desk-A").state.ui.draft, "Video fetch draft");
});

test("new-desk POST saves last edits to the old desk before clearing its canvas", async () => {
  const h = await harness(), created = deferred();
  h.setOverride(c => c.url === "/api/investigations" && c.method === "POST" ? created.promise : undefined);
  const navigation = h.window.FrameSeekPersistence.newInvestigation(); await tick();
  h.store.annotate("Edited during creation"); h.get("question").value = "Creation draft";
  const desk = { id: "created", video: "video-A", title: "New", state: {}, revision: 1 };
  h.desks.set(desk.id, desk); created.resolve(response(desk, 201)); await navigation;
  assert.equal(h.desks.get("desk-A").state.note, "Edited during creation");
  assert.equal(h.desks.get("desk-A").state.ui.draft, "Creation draft");
  assert.equal(h.window.testWorkspace.get().current.id, "created");
});

test("reopening the same desk does not restore a fetched snapshot older than the final save", async () => {
  const h = await harness(), target = deferred();
  const stale = { ...h.desks.get("desk-A"), state: structuredClone(h.desks.get("desk-A").state) };
  h.setOverride(c => c.url === "/api/investigations/desk-A" && c.method === "GET" ? target.promise : undefined);
  const navigation = h.window.testWorkspace.openDesk("desk-A"); await tick();
  h.store.annotate("Latest same-desk note"); h.get("question").value = "Latest same-desk draft";
  target.resolve(response(stale)); await navigation;
  assert.equal(h.store.getSnapshot().note, "Latest same-desk note");
  assert.equal(h.get("question").value, "Latest same-desk draft");
  assert.equal(h.window.testWorkspace.get().current.revision, h.desks.get("desk-A").revision);
});

test("same-desk reopening still accepts a newer server revision when local state is clean", async () => {
  const h = await harness();
  h.desks.get("desk-A").revision = 2;
  h.desks.get("desk-A").state = { note: "Edited by another tab", ui: { draft: "Server draft" } };
  await h.window.testWorkspace.openDesk("desk-A");
  assert.equal(h.store.getSnapshot().note, "Edited by another tab");
  assert.equal(h.get("question").value, "Server draft");
  assert.equal(h.window.testWorkspace.get().current.revision, 2);
});

test("old-desk edits during request cancellation are saved before navigation restores", async () => {
  const h = await harness({ requests: [{ id: "running", state: "running", input: { question: "Running query" } }] }), cancelled = deferred();
  h.setOverride(c => c.url.endsWith("/cancel") ? cancelled.promise : undefined);
  const navigation = h.window.testWorkspace.openDesk("desk-B"); await tick();
  h.store.annotate("Cancellation note"); h.get("question").value = "Cancellation draft";
  cancelled.resolve(response(answer("cancelled"))); await navigation;
  assert.equal(h.desks.get("desk-A").state.note, "Cancellation note");
  assert.equal(h.desks.get("desk-A").state.ui.draft, "Cancellation draft");
  assert.equal(h.get("cancel-question").hidden, true);
  assert.equal(h.get("resume-query").hidden, true);
});

test("advancing playback time alone does not cause another draining save", async () => {
  const h = await harness(), first = deferred(); const original = h.ui.snapshot; let time = 1;
  h.ui.snapshot = () => { const state = original(); return { ...state, ui: { ...state.ui, playerTime: time++ } }; };
  h.store.annotate("Durable edit");
  h.setOverride(c => c.url === "/api/investigations/desk-A" && c.method === "PUT" ? first.promise : undefined);
  const saving = h.window.FrameSeekPersistence.flush(); await tick();
  first.resolve(response({ revision: 2 })); await saving;
  const writes = h.calls.filter(c => c.url === "/api/investigations/desk-A" && c.method === "PUT");
  assert.equal(writes.length, 1); assert.ok(writes[0].body.state.ui.playerTime > 0);
});

test("deletion cancels the request before deleting and can create the fallback desk", async () => {
  const h = await harness(), query = deferred();
  h.setOverride(c => c.url === "/api/chat" ? query.promise : c.url.endsWith("/cancel") ? response(answer("cancelled")) : undefined);
  const ask = h.window.FrameSeekPersistence.ask("query"); await tick();
  h.window.testWorkspace.confirmDelete(h.desks.get("desk-A"), "investigations");
  await h.get("delete-confirm").onclick();
  const cancelIndex = h.calls.findIndex(c => c.url.endsWith("/cancel"));
  const deleteIndex = h.calls.findIndex(c => c.method === "DELETE");
  assert.ok(cancelIndex >= 0 && cancelIndex < deleteIndex);
  assert.equal(h.window.testWorkspace.get().current.video, "demo_basketball");
  query.resolve(response(answer("late"))); await ask;
  assert.equal(h.answers.length, 0);
  assert.equal(h.busy, false);
});

test("delayed recovery cannot import old answers or overwrite the new desk draft", async () => {
  const h = await harness(), recovery = deferred();
  h.desks.get("desk-B").video = "video-A";
  h.desks.get("desk-B").state.ui = { draft: "B draft" };
  h.setOverride(c => c.url === "/api/investigations/desk-A/requests" ? recovery.promise : undefined);
  const pending = h.window.testWorkspace.recover();
  await h.window.testWorkspace.openDesk("desk-B");
  recovery.resolve(response({ items: [{ id: "old", state: "done", result: answer("old") }] })); await pending;
  assert.equal(h.answers.length, 0); assert.equal(h.get("question").value, "B draft");
  assert.equal(h.window.testWorkspace.get().current.id, "desk-B");
});

test("older recovery candidates are restored without changing existing turn and finding origin IDs", async () => {
  const records = Array.from({ length: 12 }, (_, j) => ({ id: `task-${j + 2}`, query: answer(`q${j + 2}`) }));
  const pin = { id: "finding-1", kind: "conclusion", turnId: "task-2", query: records[0].query };
  const requests = Array.from({ length: 13 }, (_, j) => ({ id: `q${13 - j}`, state: "done", result: answer(`q${13 - j}`) }));
  const h = await harness({ state: { records, pins: [pin] }, requests });
  assert.equal(h.answers.length, 1);
  assert.deepEqual(h.store.getSnapshot().records.slice(0,12).map(r => r.id), records.map(r => r.id));
  assert.equal(h.answers[0].id,"q1");
  assert.equal(h.store.getSnapshot().pins[0].turnId, "task-2");
  await h.window.testWorkspace.recover(); assert.equal(h.answers.length, 1);
});

test("new recovered results preserve drafts, restore the submitted parent and persist immediately", async () => {
  const first = answer("first");
  const h = await harness({ state: { records: [{ id: "task-1", query: first }], ui: { draft: "unsent draft" } } });
  h.requestLists.set("desk-A", [{ id: "second", state: "done", input: { parent_request_id: "first" }, result: answer("second") }]);
  await h.window.testWorkspace.recover();
  assert.equal(h.answers[0].parentTurn, "task-1"); assert.equal(h.get("question").value, "unsent draft");
  assert.ok(h.desks.get("desk-A").state.records.some(r => r.query.id === "second"));
});

test('an explicit evidence seek schedules a save without continuous playback draining',async()=>{
 const h=await harness(),original=h.ui.snapshot;
 h.ui.snapshot=()=>{const state=original();return {...state,ui:{...state.ui,playerTime:88}};};
 h.window.dispatchEvent({type:'frameseek-explicit-seek'});
 await h.timer(500);await h.window.FrameSeekPersistence.flush();
 assert.equal(h.desks.get('desk-A').state.ui.playerTime,88);
});

test('refresh recovers a running supplement and later its answer without sending another supplement',async()=>{
 const h=await harness({state:{ui:{draft:'original question'}}}), prepare=deferred();
 const input={investigation_id:'desk-A',request_id:'supp-1',generation:0,question:'original question'};
 let state='running';
 h.setOverride(c=>c.url==='/api/investigations/desk-A/requests' ? response({items:[],supplements:[{id:'supp-1',state,input}]})
  : c.url==='/api/auto-chat/prepare' ? prepare.promise
  : c.url==='/api/index-chat' ? response({...answer('supp-1'),engine:'index'}) : undefined);
 await h.window.testWorkspace.recover();
 assert.equal(h.window.testWorkspace.get().activeRequest.kind,'supplement');
 assert.equal(h.get('resume-query').hidden,false);
 state='done';const recovery=h.window.testWorkspace.recover();await tick();
 h.get('question').value='new draft during recovery';
 prepare.resolve(response({kind:'ready',engine:'index'}));await recovery;
 assert.equal(h.answers[0].id,'supp-1');
 assert.equal(h.get('question').value,'new draft during recovery');
 assert.equal(h.window.testWorkspace.get().activeRequest,null);
 assert.equal(h.calls.some(c=>c.url==='/api/knowledge-chat/supplement'),false);
});

test('a recovered answer clears only its unchanged submitted question',async()=>{
 const h=await harness({state:{ui:{draft:'original question'}}});
 const input={investigation_id:'desk-A',request_id:'supp-done',generation:0,question:'original question'};
 h.setOverride(c=>c.url==='/api/investigations/desk-A/requests' ? response({items:[],supplements:[{id:'supp-done',state:'done',input}]})
  : c.url==='/api/auto-chat/prepare' ? response({kind:'ready',engine:'index'})
  : c.url==='/api/index-chat' ? response({...answer('supp-done'),engine:'index'}) : undefined);
 await h.window.testWorkspace.recover();
 assert.equal(h.answers[0].id,'supp-done');
 assert.equal(h.get('question').value,'');
 assert.equal(h.calls.some(c=>c.url==='/api/knowledge-chat/supplement'),false);
});

test('an unknown recovered supplement is not retried or treated as running',async()=>{
 const h=await harness({state:{ui:{draft:'keep question'}}});
 h.setOverride(c=>c.url==='/api/investigations/desk-A/requests'
  ? response({items:[],supplements:[{id:'supp-unknown',state:'unknown',input:{question:'keep question'}}]}) : undefined);
 await h.window.testWorkspace.recover();
 assert.equal(h.window.testWorkspace.get().activeRequest,null);
 assert.match(h.get('notice').textContent,/uncertain.*not be automatically reissued/);
 assert.equal(h.get('question').value,'keep question');
 assert.equal(h.calls.some(c=>c.url==='/api/knowledge-chat/supplement'),false);
});

test('a recovered supplement that later fails releases the composer and preserves the draft',async()=>{
 const h=await harness({state:{ui:{draft:'keep question'}}});let state='running';
 h.setOverride(c=>c.url==='/api/investigations/desk-A/requests'
  ? response({items:[],supplements:[{id:'supp-fail',state,input:{question:'keep question'}}]}) : undefined);
 await h.window.testWorkspace.recover();
 assert.equal(h.window.testWorkspace.get().activeRequest.kind,'supplement');
 state='waiting_budget';await h.window.testWorkspace.recover();
 assert.equal(h.window.testWorkspace.get().activeRequest,null);
 assert.match(h.get('notice').textContent,/pending budget or authorization/);
 assert.equal(h.get('question').value,'keep question');
});

test('cancel racing a completed supplement recovers its answer and preserves a newer draft',async()=>{
 const h=await harness({state:{ui:{draft:'newer draft'}}});let state='running';
 const input={investigation_id:'desk-A',request_id:'supp-complete',generation:0,question:'original question'};
 h.setOverride(c=>c.url==='/api/investigations/desk-A/requests'
  ? response({items:[],supplements:[{id:'supp-complete',state,input}]})
  : c.url.endsWith('/supplements/supp-complete/cancel') ? (state='done',response({state:'done'}))
  : c.url==='/api/auto-chat/prepare' ? response({kind:'ready',engine:'index'})
  : c.url==='/api/index-chat' ? response({...answer('supp-complete'),engine:'index'}) : undefined);
 await h.window.testWorkspace.recover();
 await h.get('cancel-question').onclick();
 assert.equal(h.answers[0].id,'supp-complete');
 assert.equal(h.get('question').value,'newer draft');
 assert.equal(h.window.testWorkspace.get().activeRequest,null);
 assert.equal(h.get('cancel-question').hidden,true);
 assert.equal(h.calls.some(c=>c.url==='/api/knowledge-chat/supplement'),false);
});

test('supplement recovery cannot post an answer after switching investigations',async()=>{
 const h=await harness(), wait=deferred();
 const input={investigation_id:'desk-A',request_id:'supp-late',generation:0,question:'old question'};
 h.setOverride(c=>c.url==='/api/investigations/desk-A/requests' ? response({items:[],supplements:[{id:'supp-late',state:'done',input}]})
  : c.url==='/api/auto-chat/prepare' ? wait.promise : undefined);
 const recovery=h.window.testWorkspace.recover();await tick();
 await h.window.testWorkspace.openDesk('desk-B');
 wait.resolve(response({kind:'ready',engine:'index'}));await recovery;
 assert.equal(h.calls.some(c=>c.url==='/api/index-chat'),false);
 assert.equal(h.answers.length,0);
});

test("selection changes while asking do not rewrite the actual parent or next draft", async () => {
  const h = await harness(), query = deferred();
  h.store.record("task-1", answer("first")); h.setContext(answer("first"));
  h.get("question").value = "submitted";
  h.setOverride(c => c.url === "/api/chat" ? query.promise : undefined);
  const pending = h.window.FrameSeekPersistence.ask("submitted"); await tick();
  h.store.record("task-2", answer("other")); h.get("question").value = "next question draft";
  query.resolve(response(answer("completed"))); await pending;
  assert.equal(h.calls.find(c => c.url === "/api/chat").body.parent_request_id, "first");
  assert.equal(h.answers[0].parentTurn, "task-1"); assert.equal(h.get("question").value, "next question draft");
});

test("a fresh root question does not gain a parent from the old selected record", async () => {
  const h = await harness(); h.store.record("task-1", answer("first"));
  h.setOverride(c => c.url === "/api/chat" ? response(answer("root")) : undefined);
  await h.window.FrameSeekPersistence.ask("root question");
  assert.equal(h.answers[0].parentTurn, null);
});

test("a pending request survives the polling limit and is available for recovery and cancellation", async () => {
  const h = await harness();
  h.setOverride(c => c.url === "/api/chat" ? response({ kind: "query_control", state: "pending" }) : undefined);
  const ask = h.window.FrameSeekPersistence.ask("long query"); await tick();
  for (let i = 0; i < 15; i++) await h.timer(Math.min(10000, 3000 + i * 500));
  await ask;
  assert.ok(h.window.testWorkspace.get().activeRequest); assert.equal(h.get("resume-query").hidden, false);
  const dispatched = h.calls.filter(c => c.url === "/api/chat").length;
  await h.window.FrameSeekPersistence.ask("duplicate");
  assert.equal(h.calls.filter(c => c.url === "/api/chat").length, dispatched);
  h.setOverride(c => c.url.endsWith("/cancel") ? response(answer("cancelled")) : undefined);
  await h.window.testWorkspace.openDesk("desk-B");
  assert.equal(h.window.testWorkspace.get().current.id, "desk-B"); assert.equal(h.window.testWorkspace.get().activeRequest, null);
});

test("a delayed A response cannot stop B's busy indicator or add an A result", async () => {
  const h = await harness(), a = deferred(), b = deferred(); let dispatches = 0;
  h.setOverride(c => c.url === "/api/chat" ? (++dispatches === 1 ? a.promise : b.promise) : c.url.endsWith("/cancel") ? response(answer("cancelled")) : undefined);
  const askA = h.window.FrameSeekPersistence.ask("A"); await tick();
  await h.window.testWorkspace.openDesk("desk-B");
  const askB = h.window.FrameSeekPersistence.ask("B"); await tick(); assert.equal(h.busy, true);
  a.resolve(response(answer("late-A"))); await askA;
  assert.equal(h.busy, true); assert.equal(h.answers.length, 0);
  b.resolve(response(answer("B-result", "video-B"))); await askB; assert.equal(h.busy, false);
});

test("the latest navigation wins even when an earlier desk fetch resolves last", async () => {
  const h = await harness(), a = deferred();
  h.setOverride(c => c.url === "/api/investigations/desk-A" && c.method === "GET" ? a.promise : undefined);
  const first = h.window.testWorkspace.openDesk("desk-A"); await tick();
  await h.window.testWorkspace.openDesk("desk-B");
  a.resolve(response(h.desks.get("desk-A"))); await first;
  assert.equal(h.window.testWorkspace.get().current.id, "desk-B");
});

test("source selection owns navigation before its source-to-desk lookup completes", async () => {
  const h = await harness(), firstLookup = deferred(), secondLookup = deferred(); let lookups = 0;
  h.setOverride(c => c.url === "/api/investigations" && c.method === "GET" ? (++lookups === 1 ? firstLookup.promise : secondLookup.promise) : undefined);
  const first = h.window.testWorkspace.openSource(h.videos.get("video-A")); await tick();
  const second = h.window.testWorkspace.openSource(h.videos.get("video-B")); await tick();
  firstLookup.resolve(response({ items: [...h.desks.values()] })); await first;
  secondLookup.resolve(response({ items: [...h.desks.values()] })); await second;
  assert.equal(h.window.testWorkspace.get().current.id, "desk-B");
});

test("a watcher rechecks ownership after its awaited save before restoring a source", async () => {
  const h = await harness({ job: "captioning" }), saving = deferred();
  h.store.annotate("unsaved note"); h.videos.get("video-A").job.state = "ready";
  h.setOverride(c => c.method === "PUT" && c.url.endsWith("desk-A") ? saving.promise : undefined);
  await h.timer(2000);
  const switchDesk = h.window.testWorkspace.openDesk("desk-B"); await tick();
  saving.resolve(response({ revision: 2 })); await switchDesk; await tick();
  assert.equal(h.window.testWorkspace.get().currentVideo.id, "video-B");
  assert.equal(h.store.getSnapshot().sourceInfo.id, "video-B");
});

test("reopening a processing source starts a new watcher and enables it when ready", async () => {
  const h = await harness(); h.videos.get("video-B").job.state = "indexing";
  await h.window.testWorkspace.openDesk("desk-B");
  const checks=h.calls.filter(c=>c.url==='/api/runtime/status').length;
  h.videos.get("video-B").job.state = "ready";
  h.videos.get("video-B").metadata.duration=65;
  await h.timer(2000);
  assert.equal(h.store.getSnapshot().local, false); assert.equal(h.window.testWorkspace.get().currentVideo.job.state, "ready");
  assert.equal(h.get('source-switcher').children.find(option=>option.value==='video-B').dataset.detail,'01:05 · Ready for questions');
  assert.equal(h.calls.filter(c=>c.url==='/api/runtime/status').length,checks+1);
  assert.equal(h.calls.filter(c=>c.url==='/api/chat').length,0);
});

test('long knowledge indexing remains watched after an hour and unlocks on completion', async()=>{
 const h=await harness(),video=h.videos.get('video-B');
 video.knowledge_status={required_for_chat:true,state:'indexing',completed_segments:0,total_segments:2000};
 await h.window.testWorkspace.openDesk('desk-B');
 for(let i=0;i<1801;i++) await h.timer(2000);
 video.knowledge_status={required_for_chat:true,state:'ready',completed_segments:2000,total_segments:2000};
 await h.timer(2000);
 assert.equal(h.window.testWorkspace.get().currentVideo.knowledge_status.state,'ready');
 assert.match(h.get('notice').textContent,/can start asking questions/);
 assert.equal([...h.timers.values()].some(timer=>timer.delay===2000),false);
});

test('an older watcher response cannot overwrite a replacement watcher', async()=>{
 const h=await harness(),video=h.videos.get('video-B'),old=deferred();
 video.knowledge_status={required_for_chat:true,state:'indexing',completed_segments:0,total_segments:2};
 await h.window.testWorkspace.openDesk('desk-B');
 h.setOverride(c=>c.url==='/api/videos/video-B' ? old.promise : undefined);
 await h.timer(2000);
 const epoch=h.window.testWorkspace.get().requestEpoch;
 h.window.testWorkspace.watch('video-B',epoch);
 h.setOverride(null);
 video.knowledge_status={required_for_chat:true,state:'ready',completed_segments:2,total_segments:2};
 await h.timer(2000);
 old.resolve(response({...video,knowledge_status:{required_for_chat:true,state:'indexing',completed_segments:1,total_segments:2}}));
 await tick();
 assert.equal(h.window.testWorkspace.get().currentVideo.knowledge_status.state,'ready');
});

test('knowledge progress reconnects after transient read errors without restarting work', async()=>{
 const h=await harness(),video=h.videos.get('video-B');
 video.knowledge_status={required_for_chat:true,state:'indexing',completed_segments:1,total_segments:2};
 await h.window.testWorkspace.openDesk('desk-B');
 let failures=0;
 h.setOverride(c=>c.url==='/api/videos/video-B' && failures++<2
   ? response({detail:'temporarily unavailable'},503) : undefined);
 await h.timer(2000);
 assert.match(h.get('notice').textContent,/reconnecting/);
 await h.timer(4000);await h.timer(8000);
 video.knowledge_status={required_for_chat:true,state:'ready',completed_segments:2,total_segments:2};
 await h.timer(2000);
 assert.equal(h.window.testWorkspace.get().currentVideo.knowledge_status.state,'ready');
 assert.equal(h.calls.filter(c=>c.url.includes('/knowledge/') && c.method==='POST').length,0);
});

test('knowledge progress stops on authorization failure without retrying', async()=>{
 const h=await harness(),video=h.videos.get('video-B');
 video.knowledge_status={required_for_chat:true,state:'indexing',completed_segments:1,total_segments:2};
 await h.window.testWorkspace.openDesk('desk-B');
 h.setOverride(c=>c.url==='/api/videos/video-B' ? response({detail:'expired'},401) : undefined);
 await h.timer(2000);
 assert.match(h.get('notice').textContent,/reopen the video/);
 assert.equal([...h.timers.values()].some(timer=>timer.delay===4000),false);
});

test('knowledge progress stops after bounded reconnect attempts', async()=>{
 const h=await harness(),video=h.videos.get('video-B');
 video.knowledge_status={required_for_chat:true,state:'indexing',completed_segments:1,total_segments:2};
 await h.window.testWorkspace.openDesk('desk-B');
 h.setOverride(c=>c.url==='/api/videos/video-B' ? response({detail:'offline'},503) : undefined);
 for(const delay of [2000,4000,8000,16000]) await h.timer(delay);
 assert.match(h.get('notice').textContent,/reopen the video/);
 assert.equal([...h.timers.values()].some(timer=>timer.delay===16000),false);
 assert.equal(h.calls.filter(c=>c.url.includes('/knowledge/') && c.method==='POST').length,0);
});

test("failed or cancelled preflight can be retried in fixture mode; indexed cancellation uses budget-gated analysis", async () => {
  const h = await harness({ realReady: false });
  h.videos.set("video-C", { id: "video-C", name: "C.mp4", bytes: 1, metadata: {}, job: { state: "cancelled" } });
  h.videos.get("video-B").job.state = "cancelled";
  await h.window.FrameSeekPersistence.library("videos");
  const rows = h.get("library-items").children;
  const retry = rows.find(r => r.children[0].children[0].textContent === "C.mp4").children.find(b => b.textContent === "Retry preflight");
  assert.ok(retry); assert.notEqual(retry.disabled, true);
  h.setOverride(c => c.url === "/api/videos/video-C/retry" ? response({}, 202) : undefined);
  await retry.onclick(); assert.ok(h.calls.some(c => c.url === "/api/videos/video-C/retry" && c.method === "POST"));
  const analyze = rows.find(r => r.children[0].children[0].textContent === "B.mp4").children.find(b => b.textContent === "Start analysis");
  assert.equal(analyze.disabled, true);
});

// Exercise the real app adapter, rather than mirroring its draft-clear condition.
test("app answers keep edits made while busy and recovery preserves an existing draft", async () => {
  const ids = new Map(), shell = new Element(), steps = [new Element(), new Element(), new Element()];
  const get = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
  Object.assign(get("player"), { readyState: 1, currentTime: 0, duration: 18, paused: true });
  const doc = { getElementById: get, createElement: () => new Element(),
    querySelector: selector => selector === ".agent-shell" ? shell : selector.includes("li:first-child") ? steps[0] : selector.includes("li:nth-child(2)") ? steps[1] : selector.includes("li:last-child") ? steps[2] : new Element(),
    querySelectorAll: selector => selector === ".run-steps li" ? steps : [],
  };
  const sandbox = vm.createContext({ document: doc, window: { innerWidth: 1440, addEventListener() {}, dispatchEvent() {}, matchMedia: () => ({ matches: true }) },
    fetch: async () => response({ queries: [] }), Event: class {}, URL: {}, navigator: {},
  });
  vm.runInContext(fs.readFileSync(require.resolve("../src/frameseek/static/app.js"), "utf8"), sandbox); await tick();
  vm.runInContext('setSourceOpen(false)', sandbox);
  assert.equal(shell.classList.contains('source-collapsed'), true);
  get('rail-canvas').dispatchEvent({type:'click'});
  assert.equal(shell.classList.contains('source-collapsed'), false, 'canvas restores the source player');
  assert.equal(get('rail-video').getAttribute('aria-expanded'), 'true');
  vm.runInContext('activeVideo={id:"video-A"}; window.FrameSeekSessionUI.busy(true)', sandbox);
  get("question").value = "next draft";
  vm.runInContext(`window.FrameSeekSessionUI.answer(${JSON.stringify(answer("done"))},"submitted")`, sandbox);
  assert.equal(get("question").value, "next draft"); assert.notEqual(get("question").disabled, true);
  vm.runInContext(`window.FrameSeekSessionUI.answer(${JSON.stringify(answer("recovered"))})`, sandbox);
  assert.equal(get("question").value, "next draft");
  get("question").value = "submitted";
  vm.runInContext(`window.FrameSeekSessionUI.answer(${JSON.stringify(answer("clear"))},"submitted")`, sandbox);
  assert.equal(get("question").value, "");
});

test('removed question tombstone survives recovery and navigation without redispatch or resurrection', async()=>{
  const query=answer('hidden-answer');
  const state={records:[{id:'task-4',query,hidden:true}],selectedId:null};
  const h=await harness({state,requests:[{id:query.id,state:'done',result:query}]});
  assert.equal(h.answers.length,0);
  assert.equal(h.store.getSnapshot().records[0].hidden,true);
  await h.window.testWorkspace.openDesk('desk-B');
  await h.window.testWorkspace.openDesk('desk-A');
  assert.equal(h.answers.length,0);
  assert.equal(h.store.getSnapshot().records[0].hidden,true);
  assert.equal(h.calls.some(c=>c.url==='/api/chat'),false);
});

test('player source dropdown restores each video chat and draft without model requests', async()=>{
  const h=await harness({state:{records:[{id:'task-1',query:answer('saved-A')}],ui:{draft:'draft A'}}});
  await tick();
  assert.deepEqual(h.get('source-switcher').children.map(o=>o.value),['demo_basketball','video-A','video-B']);
  h.get('source-switcher').value='video-B';
  await h.get('source-switcher').onchange();
  assert.equal(h.window.testWorkspace.get().current.id,'desk-B');
  assert.equal(h.get('question').value,'');
  h.get('question').value='draft B';
  h.get('source-switcher').value='video-A';
  await h.get('source-switcher').onchange();
  assert.equal(h.get('question').value,'draft A');
  assert.equal(h.store.getSnapshot().records[0].query.id,'saved-A');
  h.get('source-switcher').value='video-B';await h.get('source-switcher').onchange();
  assert.equal(h.get('question').value,'draft B');
  assert.equal(h.calls.some(c=>c.url==='/api/chat' || /\/ingest$/.test(c.url)),false);
  assert.equal(h.calls.some(c=>c.url==='/api/investigations' && c.method==='POST'),false);
});

test('source switcher reports knowledge progress instead of treating preflight as chat readiness', async()=>{
  const h=await harness();
  h.videos.get('video-B').knowledge_status={required_for_chat:true,state:'indexing',completed_segments:2,total_segments:3};
  await h.window.testWorkspace.refreshSourceSwitcher();
  const option=h.get('source-switcher').children.find(item=>item.value==='video-B');
  assert.match(option.dataset.detail,/Building index · 2\/3 segments/);
  assert.doesNotMatch(option.dataset.detail,/Ready for questions/);
  await h.window.FrameSeekPersistence.library('videos');
  const row=h.get('library-items').children.find(item=>item.children[0]?.children[0]?.textContent==='B.mp4');
  assert.match(row.children[0].children[1].textContent,/Building index · 2\/3 segments/);
  h.videos.get('video-B').knowledge_status.state='ready';
  await h.window.testWorkspace.refreshSourceSwitcher();
  assert.match(h.get('source-switcher').children.find(item=>item.value==='video-B').dataset.detail,/Ready for questions/);
});

test('cloud index identifies its processor and a budget wait clearly', async()=>{
  const h=await harness();
  h.videos.get('video-B').knowledge_status={required_for_chat:true,state:'waiting_budget',processor:'tokenfactory'};
  await h.window.testWorkspace.refreshSourceSwitcher();
  assert.match(h.get('source-switcher').children.find(item=>item.value==='video-B').dataset.detail,/Waiting for budget or approval/);
  h.videos.get('video-B').knowledge_status.state='ready';
  await h.window.testWorkspace.openDesk('desk-B');
  assert.match(h.get('composer-mode-text').textContent,/Cloud supplementary check requires budget access/);
  assert.doesNotMatch(h.get('composer-mode-text').textContent,/本机索引/);
});

test('failed dropdown navigation restores its selection and retains the current draft', async()=>{
  const h=await harness({state:{ui:{draft:'keep A'}}});await tick();
  h.setOverride(call=>call.url==='/api/investigations/desk-B'?response({detail:'unavailable'},503):undefined);
  h.get('source-switcher').value='video-B';await h.get('source-switcher').onchange();
  assert.equal(h.get('source-switcher').value,'video-A');
  assert.equal(h.get('source-switcher').disabled,false);
  assert.equal(h.get('question').value,'keep A');
  assert.match(h.get('notice').textContent,/Failed to switch video/);
});

test('selecting the current video does not save or create another desk',async()=>{
  const h=await harness();await tick();const before=h.calls.length;
  h.get('source-switcher').value='video-A';await h.get('source-switcher').onchange();
  assert.equal(h.calls.length,before);
});


test('a stale video-list response cannot replace the new desk selection',async()=>{
  const h=await harness();await tick();const gate=deferred();
  h.setOverride(call=>call.url==='/api/videos'?gate.promise:undefined);
  const pending=h.window.testWorkspace.refreshSourceSwitcher();
  await h.window.testWorkspace.openDesk('desk-B');
  gate.resolve(response({items:[h.videos.get('video-A')]}));await pending;
  assert.equal(h.get('source-switcher').value,'video-B');
  assert.equal(h.get('source-switcher').children.some(o=>o.value==='video-B'),true);
});

test('switching video returns to the last explicitly chosen investigation for that video',async()=>{
  const h=await harness();await tick();
  h.desks.set('other-A',{id:'other-A',video:'video-A',title:'Another A',revision:1,state:{}});
  await h.window.testWorkspace.openDesk('other-A');
  h.get('source-switcher').value='video-B';await h.get('source-switcher').onchange();
  h.get('source-switcher').value='video-A';await h.get('source-switcher').onchange();
  assert.equal(h.window.testWorkspace.get().current.id,'other-A');
});


test('custom picker receives busy and restored selection notifications around async navigation',async()=>{
  const h=await harness();await tick();const gate=deferred(), states=[];
  const select=h.get('source-switcher');
  select.addEventListener('frameseek-sources',()=>states.push({busy:select.disabled,id:select.value}));
  h.setOverride(call=>call.url==='/api/investigations/desk-B'?gate.promise:undefined);
  select.value='video-B';const pending=select.onchange();
  assert.equal(states[0].busy,true);
  assert.equal(select.disabled,true);
  gate.resolve(response({...h.desks.get('desk-B')}));await pending;
  assert.deepEqual(states.at(-1),{busy:false,id:'video-B'});
  assert.equal(select.children[0].dataset.thumbnail,'/media/demo_basketball/frames/0.jpg');
  assert.match(select.children[0].dataset.detail,/00:18/);
  assert.equal(h.calls.some(c=>c.url==='/api/chat'),false);
});

test('index search preserves draft and cannot dispatch chat; late results cannot cross desks', async()=>{
 const h=await harness();
 h.videos.get('video-A').generation=2;
 const wait=deferred();
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? wait.promise : undefined);
 h.get('question').value='找红帽子白衣服的人';
 const pending=h.get('search-index').onclick();
 assert.equal(h.get('visual-search-dialog').open,true);
 await h.window.testWorkspace.openDesk('desk-B');
 h.get('question').value='另一视频草稿';
 wait.resolve(response({indexed_frames:40,frames:[{uri:'/media/video-A/frames/f.jpg',t:1,caption:'旧视频内容'}]}));
 await pending;
 assert.equal(h.get('visual-search-dialog').open,false);
 assert.equal(h.get('visual-search-results').children.length,0);
 assert.equal(h.get('question').value,'另一视频草稿');
 assert.equal(h.calls.filter(c=>c.url==='/api/chat' || c.url==='/api/chat/prepare').length,0);
});

test('index candidates render as unverified, seek original time, and keep the question', async()=>{
 const h=await harness();let sought;
 h.videos.get('video-A').generation=2;
 h.ui.seek=t=>{sought=t;};
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({indexed_frames:40,frames:[
  {uri:'/media/video-A/frames/f.jpg',t:62.4,caption:'保存的画面描述'},
  {uri:'/media/video-B/frames/foreign.jpg',t:1,caption:'另一视频不得显示'}
 ]}) : undefined);
 h.get('question').value='我的原问题';
 await h.get('search-index').onclick();
 assert.match(h.get('visual-search-summary').textContent,/Candidates are not confirmed matches/);
 assert.equal(h.get('visual-search-results').children.length,1);
 h.get('visual-search-results').children[0].children[2].onclick();
 assert.equal(sought,62.4);
 assert.equal(h.get('question').value,'我的原问题');
 assert.equal(h.answers.length,0);
 assert.equal(h.get('visual-search-dialog').open,false);
});

test('a draft survives immediate refresh before the debounce save runs', async()=>{
 const storage=new Map(),h=await harness({storage});
 h.get('question').value='刚输入尚未保存的原问题';
 h.get('question').dispatchEvent({type:'input'});
 const reloaded=await harness({storage});
 assert.equal(reloaded.get('question').value,'刚输入尚未保存的原问题');
 await reloaded.window.FrameSeekPersistence.flush();
 assert.equal(storage.has('frameseek-draft-shadow'),false);
 assert.equal(reloaded.desks.get('desk-A').state.ui.draft,'刚输入尚未保存的原问题');
});

test('draft shadow respects ownership and never overwrites a newer server draft', async()=>{
 const storage=new Map(),h=await harness({storage});
 h.get('question').value='旧会话草稿';h.get('question').dispatchEvent({type:'input'});
 const other=await harness({storage,owner:'owner-B'});
 assert.equal(other.get('question').value,'');
 const newer=await harness({storage,state:{ui:{draft:'另一页面更新后的草稿'}}});
 assert.equal(newer.get('question').value,'另一页面更新后的草稿');
});

test('refresh restores edits typed while the preceding draft save response was in flight', async()=>{
 const storage=new Map(),h=await harness({storage}),first=deferred();
 h.get('question').value='草稿 A';h.get('question').dispatchEvent({type:'input'});
 h.setOverride(c=>c.url==='/api/investigations/desk-A' && c.method==='PUT' ? first.promise : undefined);
 const saving=h.window.FrameSeekPersistence.flush();
 h.get('question').value='草稿 B 最新编辑';h.get('question').dispatchEvent({type:'input'});
 const restored=await harness({storage,revision:2,state:{ui:{draft:'草稿 A'}}});
 assert.equal(restored.get('question').value,'草稿 B 最新编辑');
 const newer=await harness({storage,revision:3,state:{ui:{draft:'另一个页面的更新'}}});
 assert.equal(newer.get('question').value,'另一个页面的更新');
 h.setOverride(()=>undefined);first.resolve(response({revision:2}));await saving;
});

test('index results from an older generation cannot publish into the same video', async()=>{
 const h=await harness(),wait=deferred();
 h.videos.get('video-A').generation=2;
 h.get('question').value='同一视频重新预检前的问题';
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? wait.promise : undefined);
 const pending=h.get('search-index').onclick();
 h.window.testWorkspace.get().currentVideo.generation=3;
 wait.resolve(response({indexed_frames:1,frames:[{uri:'/media/video-A/frames/old.jpg',t:1,caption:'过期帧'}]}));
 await pending;
 assert.equal(h.get('visual-search-results').children.length,0);
 assert.equal(h.get('question').value,'同一视频重新预检前的问题');
});

test('full saved-index browsing loads subsequent times without sending chat or losing draft', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 const pages=[];
 h.setOverride(c=>{
  if(!c.url.endsWith('/occurrences/search'))return undefined;
  pages.push(c.body);
  return response({indexed_frames:41,total_candidates:41,snapshot:'a'.repeat(64),next_offset:c.body.offset===0 ? 40 : null,
   frames:[{uri:`/media/video-A/frames/f${c.body.offset}.jpg`,t:c.body.offset,caption:'已有描述'}]});
 });
 h.get('question').value='查找全部时间';await h.get('search-index').onclick();
 assert.equal(pages[0].limit,40);
 const more=h.get('visual-search-results').children.at(-1);
 assert.match(more.textContent,/Load later times/);await more.onclick();
 assert.equal(pages[1].offset,40);assert.equal(pages[1].snapshot,'a'.repeat(64));
 assert.equal(h.get('visual-search-results').children.length,2);
 assert.match(h.get('visual-search-summary').textContent,/do not represent every appearance/);
 assert.equal(h.get('question').value,'查找全部时间');assert.equal(h.answers.length,0);
 assert.equal(h.calls.filter(c=>c.url.startsWith('/api/chat')).length,0);
});

test('late full-index continuation cannot publish on another investigation', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;const wait=deferred();
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? c.body.offset===0 ? response({indexed_frames:41,total_candidates:41,snapshot:'a'.repeat(64),next_offset:40,frames:[]}) : wait.promise : undefined);
 h.get('question').value='查找';await h.get('search-index').onclick();
 const more=h.get('visual-search-results').children.at(-1), pending=more.onclick();
 await h.window.testWorkspace.openDesk('desk-B');
 wait.resolve(response({indexed_frames:41,frames:[{uri:'/media/video-A/frames/f.jpg',t:40,caption:'旧视频'}]}));
 await pending;
 assert.equal(h.get('visual-search-results').children.length,0);
 assert.equal(h.get('visual-search-dialog').open,false);
});

test('index chat uses separate zero-model routes and persists into existing canvas', async()=>{
 const h=await harness();h.get('chat-engine').value='index';
 h.get('question').value='find white jersey';
 h.setOverride(c=>c.url==='/api/index-chat/prepare' ? response({kind:'ready'})
  : c.url==='/api/index-chat' ? response({...answer(c.body.request_id),engine:'index',index_search:{total:3}}) : undefined);
 await h.window.FrameSeekPersistence.ask('find white jersey');
 assert.equal(h.answers.length,1);assert.equal(h.answers[0].engine,'index');
 assert.equal(h.calls.filter(c=>c.url==='/api/chat' || c.url==='/api/chat/prepare').length,0);
 assert.equal(h.calls.find(c=>c.url==='/api/index-chat').body.engine,'index');
 assert.equal(h.get('question').value,'');
});

test('stored index timeline opens without replacing the active draft', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.get('question').value='new draft';
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',
  total_occurrences:1,provenance:'ANNOTATED FIXTURE',conditions:[{field:'color',value:'white'}],
  coverage:{catalog_frames:10,reviewed_frames:10,missing_frame_ids:[],uncertain_frame_ids:[],exhaustive_under_index:true},
  occurrences:[{start:8,end:9,evidence_count:2,evidence:[{uri:'/media/video-A/frames/f.jpg',t:8}]}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('white jersey');
 assert.equal(h.get('question').value,'new draft');
 assert.match(h.get('visual-search-summary').textContent,/1 \/ 1/);
 assert.match(h.get('visual-search-coverage').textContent,/10 \/ 10/);
 assert.equal(h.get('visual-search-results').children.length,1);
});

test('sampled timeline does not present indexed observations as every video frame', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',
  total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],
  coverage:{catalog_frames:385,reviewed_frames:385,missing_frame_ids:[],uncertain_frame_ids:['f0'],catalog_complete:false,exhaustive_under_index:false},
  occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('find person');
 assert.match(h.get('visual-search-summary').textContent,/sampled frame times \(not a count of people or events\)/);
 assert.match(h.get('visual-search-coverage').textContent,/385 \/ 385 indexed frames/);
 assert.match(h.get('visual-search-coverage').textContent,/contains sampled frames, not every video frame/);
});

test('short-scope uncertain candidates are separate from matching results and can seek', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 let sought=null;h.ui.seek=time=>{sought=time;};
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',duration:10,
  total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],coverage:{catalog_frames:1,reviewed_frames:1},
  occurrences:[],uncertain_candidate_total:1,uncertain_candidates:[{t:4,frame_id:'f4',uri:'/media/video-A/frames/f4.jpg'},
    {t:5,frame_id:'foreign',uri:'/media/video-B/frames/f5.jpg'}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('在00:04找白色球衣的人');
 const list=h.get('visual-search-results'),group=list.children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames');
 assert.ok(group.children.some(item=>/Unverified candidate/.test(item.textContent)));
 const candidate=group.children.find(item=>item.children[0]?.src==='/media/video-A/frames/f4.jpg');
 assert.ok(candidate);assert.match(candidate.children[1].textContent,/some conditions are unclear/);
 candidate.children[2].onclick();assert.equal(sought,4);
 assert.equal(group.children.some(item=>item.children[0]?.src==='/media/video-B/frames/f5.jpg'),false);
});

test('full-video uncertain candidates load additional pages with the same snapshot', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',duration:30,
  snapshot:'a'.repeat(64),knowledge_version:'b'.repeat(64),total_occurrences:0,provenance:'fixture',
  conditions:[],available_conditions:[],coverage:{catalog_frames:20,reviewed_frames:20},occurrences:[],
  uncertain_candidate_total:9,uncertain_candidate_next_offset:c.body.candidate_offset ? null : 8,
  uncertain_candidates:[{t:c.body.candidate_offset || 0,uri:`/media/video-A/frames/${c.body.candidate_offset || 0}.jpg`}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('find a person wearing white');
 const list=h.get('visual-search-results'),group=list.children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames'),more=group.children.find(item=>/Load more unverified frames/.test(item.textContent));
 assert.ok(more);await more.onclick();
 const calls=h.calls.filter(c=>c.url.endsWith('/occurrences/search'));
 assert.equal(calls.at(-1).body.candidate_offset,8);
 assert.equal(calls.at(-1).body.snapshot,'a'.repeat(64));
 assert.ok(group.children.some(item=>item.children[0]?.src==='/media/video-A/frames/8.jpg'));
 assert.equal(group.children.some(item=>/Load more unverified frames/.test(item.textContent)),false);
});

test('later matching pages stay above the separate candidate section', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',duration:30,
  snapshot:'a'.repeat(64),total_occurrences:2,provenance:'fixture',conditions:[],available_conditions:[],coverage:{},
  next_offset:c.body.offset ? null : 1,occurrences:[{start:c.body.offset ? 4 : 1,end:null,evidence_count:1,
   evidence:[{t:c.body.offset ? 4 : 1,uri:`/media/video-A/frames/${c.body.offset ? 4 : 1}.jpg`}]}],
  uncertain_candidate_total:1,uncertain_candidates:c.body.offset ? [] : [{t:8,uri:'/media/video-A/frames/candidate.jpg'}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('find a person');
 const list=h.get('visual-search-results'),more=list.children.find(item=>/Load later times/.test(item.textContent));
 await more.onclick();
 const groupIndex=list.children.findIndex(item=>item.getAttribute('aria-label')==='Unverified candidate frames');
 const matchIndex=list.children.findIndex(item=>item.children[0]?.src==='/media/video-A/frames/4.jpg');
 assert.ok(matchIndex>=0 && groupIndex>matchIndex);
 assert.equal(list.children[groupIndex].children.some(item=>item.children[0]?.src==='/media/video-A/frames/candidate.jpg'),true);
});

test('candidate time jump uses the pinned snapshot without model dispatch', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',duration:400,
  snapshot:'a'.repeat(64),knowledge_version:'b'.repeat(64),total_occurrences:0,provenance:'fixture',
  conditions:[],available_conditions:[],coverage:{},occurrences:[],uncertain_candidate_total:9,
  uncertain_candidate_next_offset:null,uncertain_candidates:[{t:c.body.candidate_time || 0,
   uri:`/media/video-A/frames/${c.body.candidate_time || 0}.jpg`}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('find a person wearing white');
 const group=h.get('visual-search-results').children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames');
 const controls=group.children.find(item=>item.className==='candidate-time-controls');
 controls.children[0].value='05:20';await controls.children[1].onclick();
 const request=h.calls.filter(c=>c.url.endsWith('/occurrences/search')).at(-1);
 assert.equal(request.body.candidate_time,320);assert.equal(request.body.snapshot,'a'.repeat(64));
 assert.ok(group.children.some(item=>item.children[0]?.src==='/media/video-A/frames/320.jpg'));
 assert.equal(h.calls.some(c=>c.url.includes('/knowledge/') && c.method==='POST'),false);
});

test('a late candidate page cannot replace a newer time jump', async()=>{
 const h=await harness(),pending=deferred();h.videos.get('video-A').generation=2;
 h.setOverride(c=>{
  if(!c.url.endsWith('/occurrences/search'))return undefined;
  if(c.body.candidate_offset)return pending.promise;
  return response({kind:'indexed_occurrences',duration:400,snapshot:'a'.repeat(64),knowledge_version:'b'.repeat(64),
   total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],coverage:{},occurrences:[],
   uncertain_candidate_total:9,uncertain_candidate_next_offset:c.body.candidate_time!=null ? null : 8,
   uncertain_candidates:[{t:c.body.candidate_time || 0,uri:`/media/video-A/frames/${c.body.candidate_time || 0}.jpg`}]});
 });
 await h.window.FrameSeekPersistence.searchIndex('find a person');
 const group=h.get('visual-search-results').children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames');
 const older=group.children.find(item=>/Load more unverified frames/.test(item.textContent)).onclick();
 const controls=group.children.find(item=>item.className==='candidate-time-controls');
 controls.children[0].value='05:20';await controls.children[1].onclick();
 pending.resolve(response({snapshot:'a'.repeat(64),knowledge_version:'b'.repeat(64),
  uncertain_candidates:[{t:8,uri:'/media/video-A/frames/stale.jpg'}]}));
 await older;
 assert.ok(group.children.some(item=>item.children[0]?.src==='/media/video-A/frames/320.jpg'));
 assert.equal(group.children.some(item=>item.children[0]?.src==='/media/video-A/frames/stale.jpg'),false);
});

test('late candidate pages cannot overwrite a newer attribute filter', async()=>{
 const h=await harness(),wait=deferred();h.videos.get('video-A').generation=2;
 const coverage={catalog_frames:20,reviewed_frames:20};
 h.setOverride(c=>{
  if(!c.url.endsWith('/occurrences/search'))return undefined;
  if(c.body.candidate_offset)return wait.promise;
  return response({kind:'indexed_occurrences',duration:30,snapshot:'a'.repeat(64),knowledge_version:'b'.repeat(64),
   total_occurrences:0,provenance:'fixture',conditions:c.body.conditions || [],
   available_conditions:[{field:'kind',value:'person',operator:'is'}],coverage,occurrences:[],
   uncertain_candidate_total:9,uncertain_candidate_next_offset:c.body.conditions ? null : 8,
   uncertain_candidates:[{t:0,uri:'/media/video-A/frames/current.jpg'}]});
 });
 await h.window.FrameSeekPersistence.searchIndex('find a person');
 const list=h.get('visual-search-results'),group=list.children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames'),more=group.children.find(item=>/Load more unverified frames/.test(item.textContent));
 const pending=more.onclick();
 h.get('visual-search-options').children[0].firstElementChild.checked=true;
 h.get('visual-search-apply').onclick();await tick();
 wait.resolve(response({snapshot:'a'.repeat(64),knowledge_version:'b'.repeat(64),
  uncertain_candidates:[{t:8,uri:'/media/video-A/frames/stale.jpg'}]}));
 await pending;
 const current=list.children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames');
 assert.equal(current.children.some(item=>item.children[0]?.src==='/media/video-A/frames/stale.jpg'),false);
 assert.ok(current.children.some(item=>item.children[0]?.src==='/media/video-A/frames/current.jpg'));
});

test('candidate pages from a changed index version are not mixed', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',duration:30,
  snapshot:'a'.repeat(64),knowledge_version:c.body.candidate_offset ? 'c'.repeat(64) : 'b'.repeat(64),
  total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],coverage:{},occurrences:[],
  uncertain_candidate_total:9,uncertain_candidate_next_offset:c.body.candidate_offset ? null : 8,
  uncertain_candidates:[{t:c.body.candidate_offset || 0,uri:`/media/video-A/frames/${c.body.candidate_offset || 0}.jpg`}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('find a person');
 const list=h.get('visual-search-results'),group=list.children.find(item=>item.getAttribute('aria-label')==='Unverified candidate frames'),more=group.children.find(item=>/Load more unverified frames/.test(item.textContent));
 await more.onclick();
 assert.equal(group.children.some(item=>item.children[0]?.src==='/media/video-A/frames/8.jpg'),false);
 assert.match(h.get('visual-search-coverage').textContent,/Failed to load candidate frames/);
});

test('saved answer timeline keeps its request anchor when browsing the index', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',total_occurrences:0,
  conditions:[],available_conditions:[],coverage:{},occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'saved question',answerRequestId:'saved-id'});
 const request=h.calls.find(c=>c.url.endsWith('/occurrences/search'));
 assert.equal(request.body.question,'saved question');assert.equal(request.body.answer_request_id,'saved-id');
});

test('unversioned historical answer offers explicit latest-index lookup', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ?
  c.body.answer_request_id ? response({detail:'index_answer_version_unavailable'},409)
   : response({kind:'indexed_occurrences',knowledge_version:'latest',total_occurrences:0,
      conditions:[],available_conditions:[],coverage:{},occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'find person',answerRequestId:'legacy'});
 assert.match(h.get('visual-search-summary').textContent,/does not have an indexed version saved/);
 const latest=h.get('visual-search-results').children.find(item=>item.textContent==='View the latest index');
 assert.ok(latest);latest.onclick();await tick();
 assert.equal(h.calls.filter(c=>c.url.endsWith('/occurrences/search')).at(-1).body.answer_request_id,null);
 assert.match(h.get('visual-search-summary').textContent,/Existing observations match/);
});

test('visible text substring conditions are labelled as contains', async()=>{
 const h=await harness();h.videos.get('video-A').generation=2;
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',
  total_occurrences:0,provenance:'fixture',conditions:[{field:'text',operator:'contains',value:'EXIT'}],
  coverage:{catalog_frames:1,reviewed_frames:1,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false},
  occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('找出文字“EXIT”');
 assert.match(h.get('visual-search-conditions').textContent,/text contains EXIT/);
});

test('late OCR candidates cannot replace a newer attribute filter', async()=>{
 const h=await harness(), wait=deferred();
 const video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 const coverage={catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false};
 h.setOverride(c=>c.url.endsWith('/ocr-candidates') ? wait.promise : c.url.endsWith('/occurrences/search') ? response({
  kind:'indexed_occurrences',ocr_eligible:true,total_occurrences:c.body.conditions ? 1 : 0,provenance:'fixture',conditions:c.body.conditions || [],
  available_conditions:[{field:'kind',value:'person',operator:'is'}],coverage,
  occurrences:c.body.conditions ? [{start:1,end:null,evidence_count:1,evidence:[{uri:'/media/video-A/frames/person.jpg',t:1}]}] : []
 }) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('请在00:00至00:02找出文字“EXIT”');
 const button=h.get('visual-search-results').children.at(-1);
 assert.match(button.textContent,/OCR/);const pending=button.onclick();
 h.get('visual-search-options').children[0].firstElementChild.checked=true;
 h.get('visual-search-apply').onclick();await tick();
 assert.match(h.get('visual-search-summary').textContent,/1 \/ 1/);
 wait.resolve(response({frames:[{uri:'/media/video-A/frames/stale.jpg',t:0}],checked_frames:2,failed_frames:0}));
 await pending;
 assert.match(h.get('visual-search-summary').textContent,/1 \/ 1/);
 assert.equal(h.get('visual-search-results').children.length,1);
 assert.equal(h.get('visual-search-results').children[0].children[0].src,'/media/video-A/frames/person.jpg');
});

test('OCR candidates supplement existing observations without replacing them', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/ocr-candidates') ? response({knowledge_version:'same-version',frames:[{frame_id:'same',uri:'/media/video-A/frames/observed.jpg',t:0},{frame_id:'extra',uri:'/media/video-A/frames/ocr.jpg',t:1}],checked_frames:2,failed_frames:0})
  : c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:true,knowledge_version:'same-version',total_occurrences:1,provenance:'fixture',conditions:[],available_conditions:[],
   coverage:{catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false},
   occurrences:[{start:0,end:null,evidence_count:1,evidence:[{frame_id:'same',uri:'/media/video-A/frames/observed.jpg',t:0}]}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex('请在00:00至00:02找出文字“EXIT”');
 const list=h.get('visual-search-results'),button=list.children[0];
 assert.match(button.textContent,/OCR/);await button.onclick();
 assert.equal(list.children.length,2);
 assert.equal(list.children[0].children[0].src,'/media/video-A/frames/observed.jpg');
 assert.equal(list.children[1].children[0].src,'/media/video-A/frames/ocr.jpg');
 assert.match(list.children[1].children[1].textContent,/candidate.*verified/);
 assert.match(h.get('visual-search-summary').textContent,/Existing index results are retained/);
 assert.match(h.get('visual-search-summary').textContent,/found 1 additional/);
 assert.match(h.get('visual-search-coverage').textContent,/fixture · Reviewed observations for 2 \/ 2/);
 assert.match(h.get('visual-search-coverage').textContent,/OCR checked 2 sampled frames/);
});

test('direct text-candidate action runs bounded OCR once and labels the result unverified', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/ocr-candidates') ? response({knowledge_version:'version',
   frames:[{frame_id:'candidate',uri:'/media/video-A/frames/ocr.jpg',t:1}],checked_frames:2,failed_frames:0})
  : c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:true,knowledge_version:'version',
   total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],
   coverage:{catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false},occurrences:[]}) : undefined);
 const question='请在00:00至00:02找出文字“EXIT”';
 assert.equal(h.window.FrameSeekPersistence.canSearchOcr(question),true);
 await h.window.FrameSeekPersistence.searchIndex({question,answerRequestId:'old-answer',autoOcr:true});
 assert.equal(h.calls.filter(c=>c.url.endsWith('/ocr-candidates')).length,1);
 assert.match(h.get('visual-search-summary').textContent,/unverified candidate/);
 assert.equal(h.get('visual-search-results').children.some(row=>row.children?.some(child=>child.src?.endsWith('/ocr.jpg'))),true);
 assert.equal(h.calls.some(c=>c.url==='/api/knowledge-chat/supplement'),false);
});

test('direct OCR retains indexed hits and does not duplicate matching frame candidates', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/ocr-candidates') ? response({knowledge_version:'version',frames:[
  {frame_id:'same',uri:'/media/video-A/frames/observed.jpg',t:0},
  {frame_id:'extra',uri:'/media/video-A/frames/ocr.jpg',t:1}],checked_frames:2,failed_frames:0})
  : c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:true,knowledge_version:'version',
   total_occurrences:1,provenance:'fixture',conditions:[],available_conditions:[],
   coverage:{catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false},
   occurrences:[{start:0,end:null,evidence_count:1,evidence:[{frame_id:'same',uri:'/media/video-A/frames/observed.jpg',t:0}]}]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'请在00:00至00:02找出文字“EXIT”',answerRequestId:'old-answer',autoOcr:true});
 const rows=h.get('visual-search-results').children;
 assert.deepEqual(rows.map(row=>row.children?.[0]?.src).filter(Boolean).sort(),
  ['/media/video-A/frames/observed.jpg','/media/video-A/frames/ocr.jpg'].sort());
 assert.equal(h.calls.filter(c=>c.url.endsWith('/ocr-candidates')).length,1);
 assert.match(h.get('visual-search-summary').textContent,/found 1 additional unverified candidate/);
});

test('OCR candidates from a different index version do not mix with a historical answer', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/ocr-candidates') ? response({knowledge_version:'new-version',
   frames:[{frame_id:'new',uri:'/media/video-A/frames/new.jpg',t:1}],checked_frames:2,failed_frames:0})
  : c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:true,knowledge_version:'old-version',
   total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],
   coverage:{catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false},occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'请在00:00至00:02找出文字“EXIT”',answerRequestId:'old-answer'});
 const list=h.get('visual-search-results');
 const button=list.children.find(item=>/OCR/.test(item.textContent));await button.onclick();
 assert.match(h.get('visual-search-summary').textContent,/Index version has changed/);
 assert.equal(list.children.some(row=>row.children?.some(child=>child.src?.endsWith('/new.jpg'))),false);
 const latest=list.children.find(item=>item.textContent==='View the latest index');assert.ok(latest);latest.onclick();await tick();
 const searches=h.calls.filter(c=>c.url.endsWith('/occurrences/search'));
 assert.equal(searches.at(-1).body.answer_request_id,null);
});

test('OCR does not mix versioned candidates into an unversioned legacy result', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/ocr-candidates') ? response({knowledge_version:'current-version',
   frames:[{frame_id:'new',uri:'/media/video-A/frames/new.jpg',t:1}],checked_frames:2,failed_frames:0})
  : c.url.endsWith('/occurrences/search') ? response({kind:'index_candidates',ocr_eligible:true,frames:[],total_candidates:0,
   indexed_frames:2,terms:['EXIT'],available_conditions:[],coverage:{}}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'请在00:00至00:02找出文字“EXIT”',answerRequestId:'legacy',autoOcr:true});
 const list=h.get('visual-search-results');
 assert.match(h.get('visual-search-summary').textContent,/Index version has changed/);
 assert.equal(list.children.some(row=>row.children?.some(child=>child.src?.endsWith('/new.jpg'))),false);
 assert.ok(list.children.find(item=>item.textContent==='View the latest index'));
});

test('OCR action offers only short valid English and Chinese time ranges', async()=>{
 for(const [question,offered] of [
  ['Find text "EXIT" from 00:04 to 00:06',true],
  ['Find text "EXIT" between 00:04 and 00:06',true],
  ['Find text "EXIT" at 00:04',true],
  ['在00:04找文字“EXIT”',true],
  ['在00:04附近找文字“EXIT”',false],
  ['在00:04之前找文字“EXIT”',false],
  ['Find text "EXIT" not at 00:04',false],
  ['Find text "EXIT" at 00:18',false],
  ['Find text "00:04 to 00:06" from 00:08 to 00:10',true],
  ['Find text "00:04 to 00:06"',false],
  ['找文字“EXIT” 00:04至00:06',true],
  ['Find text "EXIT" from 00:00 to 00:09',false],
  ['Find text "EXIT" 00:04 and 00:06',false],
  ['Find text "EXIT" from 00:04 to 00:06 and 00:07',false],
  ['Find text "EXIT" from 00:04 to 00:20',false],
 ]) {
  const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
  video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
  h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:true,total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],
   coverage:{catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:['f0'],exhaustive_under_index:false},occurrences:[]}) : undefined);
  await h.window.FrameSeekPersistence.searchIndex(question);
  assert.equal(h.get('visual-search-results').children.some(item=>/OCR/.test(item.textContent)),offered,question);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/ocr-candidates')).length,0);
 }
});

test('server-ineligible OCR never dispatches even if an older answer offered the action', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:false,
  knowledge_version:'version',total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],
  coverage:{catalog_frames:1,reviewed_frames:1},occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'在00:04找红色文字“EXIT”',answerRequestId:'old',autoOcr:true});
 assert.equal(h.calls.filter(c=>c.url.endsWith('/ocr-candidates')).length,0);
 assert.equal(h.get('visual-search-results').children.some(item=>/OCR/.test(item.textContent)),false);
 assert.match(h.get('visual-search-coverage').textContent,/Text candidates are unavailable/);
});

test('stale OCR action explains when OCR is no longer enabled', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:false};
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'indexed_occurrences',ocr_eligible:true,
  knowledge_version:'version',total_occurrences:0,provenance:'fixture',conditions:[],available_conditions:[],
  coverage:{catalog_frames:1,reviewed_frames:1},occurrences:[]}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'在00:04找文字“EXIT”',answerRequestId:'old',autoOcr:true});
 assert.equal(h.calls.filter(c=>c.url.endsWith('/ocr-candidates')).length,0);
 assert.match(h.get('visual-search-coverage').textContent,/OCR is not enabled for the current service/);
});

test('stale OCR action explains when index search requires clarification', async()=>{
 const h=await harness(),video=h.videos.get('video-A');video.generation=2;video.metadata.duration=18;
 video.knowledge_status={required_for_chat:true,state:'ready',ocr_candidates_available:true};
 h.setOverride(c=>c.url.endsWith('/occurrences/search') ? response({kind:'clarification_required',message:'请明确条件'}) : undefined);
 await h.window.FrameSeekPersistence.searchIndex({question:'在00:04找文字“EXIT”',answerRequestId:'old',autoOcr:true});
 assert.equal(h.calls.filter(c=>c.url.endsWith('/ocr-candidates')).length,0);
 assert.match(h.get('visual-search-coverage').textContent,/text candidate is not running/);
});

test('late index pagination cannot overwrite a newer attribute filter', async()=>{
 const h=await harness(), wait=deferred();h.videos.get('video-A').generation=2;
 const coverage={catalog_frames:2,reviewed_frames:2,missing_frame_ids:[],uncertain_frame_ids:[],exhaustive_under_index:false};
 h.setOverride(c=>{
  if(!c.url.endsWith('/occurrences/search'))return undefined;
  if(c.body.offset)return wait.promise;
  return response({kind:'indexed_occurrences',total_occurrences:1,provenance:'fixture',coverage,
   conditions:c.body.conditions || [],available_conditions:[{field:'kind',value:'person',operator:'is'}],
   next_offset:c.body.conditions ? null : 1,snapshot:'a'.repeat(64),
   occurrences:[{start:1,end:null,evidence_count:1,evidence:[{uri:'/media/video-A/frames/person.jpg',t:1}]}]});
 });
 await h.window.FrameSeekPersistence.searchIndex('find a person');
 const pending=h.get('visual-search-results').children.at(-1).onclick();
 h.get('visual-search-options').children[0].firstElementChild.checked=true;
 h.get('visual-search-apply').onclick();await tick();
 wait.resolve(response({kind:'indexed_occurrences',total_occurrences:1,provenance:'old',coverage,
  occurrences:[{start:0,end:null,evidence_count:1,evidence:[{uri:'/media/video-A/frames/stale.jpg',t:0}]}]}));
 await pending;
 assert.equal(h.get('visual-search-results').children.length,1);
 assert.equal(h.get('visual-search-results').children[0].children[0].src,'/media/video-A/frames/person.jpg');
 assert.match(h.get('visual-search-coverage').textContent,/fixture/);
});

test('owned history snapshots send references while retaining full client answers and drafts',async()=>{
 const q=answer('durable-source-query');
 const h=await harness({queryReferences:true,state:{records:[{id:'task-1',query:q}],pins:[]}});
 h.get('question').value='keep my draft';
 await h.window.FrameSeekPersistence.flush();
 const saved=h.calls.filter(c=>c.method==='PUT').at(-1).body.state;
 assert.deepEqual(saved.records[0].query,{id:q.id});
 assert.equal(saved.ui.draft,'keep my draft');
 assert.equal(h.store.getSnapshot().records[0].query.response.answer,q.response.answer);
});


test("stop processing preserves draft, permits a new request and discards late old output", async () => {
  const h = await harness(), first = deferred(), second = deferred();
  h.get("chat-engine").value = "local";
  let requests = 0;
  h.setOverride(c => {
    if(c.url === "/api/local-chat/prepare")return response({kind:"ready"});
    if(c.url === "/api/local-chat")return ++requests === 1 ? first.promise : second.promise;
    if(c.url.endsWith("/cancel"))return response({cancelled:true});
  });
  h.get("question").value = "first";
  const old = h.window.FrameSeekPersistence.ask("first"); await tick();
  assert.equal(h.get("cancel-question").hidden,false);
  h.get("question").value = "next draft";
  await h.get("cancel-question").onclick();
  assert.equal(h.get("question").value,"next draft");
  assert.equal(h.busy,false);
  assert.equal(h.get("cancel-question").hidden,true);
  const next = h.window.FrameSeekPersistence.ask("next draft"); await tick();
  assert.equal(h.busy,true);
  first.resolve(response(answer("late-old"))); await old;
  assert.equal(h.answers.length,0);
  assert.equal(h.busy,true,"old completion cannot unlock the new request");
  second.resolve(response(answer("new-answer"))); await next;
  assert.deepEqual(h.answers.map(r=>r.id),["new-answer"]);
  assert.equal(h.busy,false);
  assert.equal(h.calls.filter(c=>c.url.endsWith("/cancel")).length,1);
});

for (const status of ["ok", "partial"]) {
  test(`stopping retains a ${status} query result returned by cancellation`, async () => {
    const h = await harness(), query = deferred();
    h.store.record("parent-turn", answer("parent"));
    h.setContext({mode:"real",id:"parent"});
    let requestId;
    h.setOverride(c => {
      if (c.url === "/api/chat") { requestId=c.body.request_id; return query.promise; }
      if (c.url.endsWith("/cancel")) {
        const result=answer(requestId);
        result.response.status=status;
        result.response.timebase="video_absolute_seconds";
        result.response.trace={visible:{channels:["visual_vector"],segments_reviewed:1,evidence_segments:1},debug:null};
        result.response.events=[{video_id:"video-A",start:1,end:2,start_hms:"00:00:01",end_hms:"00:00:02",action:"visible action",summary:"observed",support_level:"high",evidence:[{t:1,t_hms:"00:00:01",frame_id:"video-A_frame-1",uri:"/media/video-A/frames/frame-1.jpg",caption:"visible action",asr:null}]}];
        if(status==="partial")result.response.unresolved=["more evidence needed"];
        return response(result);
      }
    });
    h.get("question").value="original";
    const old=h.window.FrameSeekPersistence.ask("original"); await tick();
    h.get("question").value="new draft";
    await h.get("cancel-question").onclick();
    assert.equal(h.get("question").value,"new draft");
    assert.equal(h.answers.length,1);
    assert.equal(h.answers[0].id,requestId);
    assert.equal(h.answers[0].parentTurn,"parent-turn");
    assert.equal(h.answers[0].response.status,status);
    assert.ok(h.desks.get("desk-A").state.records.some(r=>r.query.id===requestId));
    query.resolve(response(answer(requestId))); await old;
    assert.equal(h.answers.length,1,"a late original response cannot duplicate the result");
  });
}

test("a cancelled query without evidence does not create a result card", async () => {
  const h=await harness(), query=deferred(); let requestId;
  h.setOverride(c=>{
    if(c.url==="/api/chat"){requestId=c.body.request_id;return query.promise;}
    if(c.url.endsWith("/cancel")){
      const result=answer(requestId);result.response.status="error";
      result.response.errors=[{code:"query_cancelled"}];return response(result);
    }
  });
  const old=h.window.FrameSeekPersistence.ask("original");await tick();
  await h.get("cancel-question").onclick();
  assert.equal(h.answers.length,0);
  assert.match(h.get("notice").textContent,/has been stopped/);
  query.resolve(response(answer(requestId)));await old;
  assert.equal(h.answers.length,0);
});

test("recovery excludes empty cancellations but retains partial and genuine errors", async () => {
  const h=await harness();
  const cancelled=answer("cancelled"), partial=answer("partial"), failure=answer("failure");
  cancelled.response.status="error";
  cancelled.response.errors=[{code:"query_cancelled"}];
  partial.response.status="partial";
  partial.response.events=[{video_id:"video-A",start:1,end:2,evidence:[{frame_id:"video-A_frame-1",t:1,uri:"/media/video-A/frames/frame-1.jpg"}]}];
  partial.response.unresolved=["more evidence needed"];
  failure.response.status="error";
  failure.response.errors=[{code:"query_cancelled"},{code:"model_unknown"}];
  h.requestLists.set("desk-A",[cancelled,partial,failure].map(result=>({id:result.id,state:"done",result})));
  h.get("question").value="new draft";
  await h.window.testWorkspace.recover();
  await h.window.testWorkspace.recover();
  assert.deepEqual(h.answers.map(result=>result.id),["failure","partial"]);
  assert.equal(h.answers.find(result=>result.id==="partial").response.events.length,1);
  assert.equal(h.get("question").value,"new draft");
});

test("an empty cancellation stays hidden after reopening the investigation", async () => {
  const h=await harness(), query=deferred(); let requestId;
  h.setOverride(c=>{
    if(c.url==="/api/chat"){requestId=c.body.request_id;return query.promise;}
    if(c.url.endsWith("/cancel")){
      const result=answer(requestId);result.response.status="error";
      result.response.errors=[{code:"query_cancelled"}];
      h.requestLists.set("desk-A",[{id:requestId,state:"done",result}]);
      return response(result);
    }
  });
  const pending=h.window.FrameSeekPersistence.ask("original");await tick();
  await h.get("cancel-question").onclick();
  await h.window.testWorkspace.openDesk("desk-B");
  await h.window.testWorkspace.openDesk("desk-A");
  assert.equal(h.answers.length,0);
  assert.equal(h.store.getSnapshot().records.length,0);
  query.resolve(response(answer(requestId)));await pending;
  assert.equal(h.answers.length,0);
});

test("stopping a recovered request retains its original parent", async () => {
  const h=await harness({requests:[{id:"resumed",state:"running",input:{question:"follow-up",parent_request_id:"parent"}}]});
  h.store.record("parent-turn",answer("parent"));
  h.get("question").value="new draft";
  h.setOverride(c=>c.url.endsWith("/cancel") ? response(answer("resumed")) : undefined);
  await h.get("cancel-question").onclick();
  assert.equal(h.answers[0].parentTurn,"parent-turn");
  assert.equal(h.get("question").value,"new draft");
});

test("resetting the investigation while cancellation is pending rejects its old result", async () => {
  const h=await harness(),query=deferred(),cancel=deferred();let requestId;
  h.setOverride(c=>{
    if(c.url==="/api/chat"){requestId=c.body.request_id;return query.promise;}
    if(c.url.endsWith("/cancel"))return cancel.promise;
  });
  const old=h.window.FrameSeekPersistence.ask("original");await tick();
  const stopping=h.get("cancel-question").onclick();await tick();
  h.store.clear();
  cancel.resolve(response(answer(requestId)));await stopping;
  assert.equal(h.answers.length,0);
  query.resolve(response(answer(requestId)));await old;
  assert.equal(h.answers.length,0);
});

test("navigation saves a cancellation result on the old desk without importing it into the new desk", async () => {
  const h=await harness(), query=deferred(), firstCancel=deferred(), navigationCancel=deferred();
  let requestId,cancels=0;
  h.setOverride(c=>{
    if(c.url==="/api/chat"){requestId=c.body.request_id;return query.promise;}
    if(c.url.endsWith("/cancel"))return ++cancels===1 ? firstCancel.promise : navigationCancel.promise;
  });
  h.get("question").value="original";
  const old=h.window.FrameSeekPersistence.ask("original");await tick();
  h.get("question").value="old desk draft";
  const stopping=h.get("cancel-question").onclick();await tick();
  const navigation=h.window.testWorkspace.openDesk("desk-B");await tick();
  assert.equal(cancels,2);
  firstCancel.resolve(response(answer(requestId)));await stopping;
  navigationCancel.resolve(response(answer(requestId)));await navigation;
  assert.equal(h.window.testWorkspace.get().current.id,"desk-B");
  assert.equal(h.store.getSnapshot().records.length,0);
  assert.equal(h.desks.get("desk-A").state.ui.draft,"old desk draft");
  assert.ok(h.desks.get("desk-A").state.records.some(r=>r.query.id===requestId));
  query.resolve(response(answer(requestId)));await old;
  assert.equal(h.store.getSnapshot().records.length,0);
});

test("a late cancellation cannot unlock or hide controls for a newer query", async () => {
  const h=await harness(),first=deferred(),second=deferred(),cancel=deferred();let requests=0,firstId;
  h.setOverride(c=>{
    if(c.url==="/api/chat"){
      if(++requests===1){firstId=c.body.request_id;return first.promise;}
      return second.promise;
    }
    if(c.url.endsWith("/cancel"))return cancel.promise;
  });
  const old=h.window.FrameSeekPersistence.ask("first");await tick();
  const stopping=h.get("cancel-question").onclick();await tick();
  first.resolve(response(answer(firstId)));await old;
  h.get("question").value="new draft";
  const next=h.window.FrameSeekPersistence.ask("new draft");await tick();
  assert.equal(h.busy,true);
  assert.equal(h.get("cancel-question").hidden,false);
  cancel.resolve(response(answer(firstId)));await stopping;
  assert.equal(h.busy,true);
  assert.equal(h.get("cancel-question").hidden,false);
  assert.equal(h.get("question").value,"new draft");
  second.resolve(response(answer("new-answer")));await next;
  assert.deepEqual(h.answers.map(r=>r.id),[firstId,"new-answer"]);
});


test('automatic plan selects one route and dispatches once without changing manual preference',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='find jersey';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'ready',engine:'index',route_message:'reuse checked observations'})
  : c.url==='/api/index-chat' ? response({...answer(c.body.request_id),engine:'index'}) : undefined);
 await h.window.FrameSeekPersistence.ask('find jersey');
 assert.equal(h.answers.length,1);assert.equal(h.get('chat-engine').value,'auto');
 assert.equal(h.calls.filter(c=>c.url==='/api/auto-chat/prepare').length,1);
 assert.equal(h.calls.filter(c=>c.url==='/api/index-chat').length,1);
 assert.equal(h.calls.some(c=>c.url==='/api/chat' || c.url==='/api/local-chat' || c.url==='/api/cloud-chat'),false);
});
test('automatic cloud budget denial preserves draft and never dispatches or falls back',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='original draft';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'budget_blocked',engine:'cloud',message:'budget denied'}) : undefined);
 await h.window.FrameSeekPersistence.ask('original draft');
 assert.equal(h.get('question').value,'original draft');assert.equal(h.answers.length,0);assert.equal(h.busy,false);
 assert.equal(h.calls.some(c=>['/api/chat','/api/local-chat','/api/cloud-chat','/api/index-chat'].includes(c.url)),false);
});
test('automatic planning response is ignored after navigating away',async()=>{
 const h=await harness(),wait=deferred();h.get('chat-engine').value='auto';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? wait.promise : undefined);
 const pending=h.window.FrameSeekPersistence.ask('old question');await tick();
 await h.window.testWorkspace.openDesk('desk-B');
 wait.resolve(response({kind:'ready',engine:'local'}));await pending;
 assert.equal(h.calls.some(c=>c.url==='/api/local-chat'),false);assert.equal(h.answers.length,0);
});
test('automatic supplement updates the index once before answering',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='find red hat';let plans=0;
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response(++plans===1
  ? {kind:'supplement_required',engine:'index'} : {kind:'ready',engine:'index'})
  : c.url==='/api/knowledge-chat/supplement' ? response({state:'done'})
  : c.url==='/api/index-chat' ? response({...answer(c.body.request_id),engine:'index'}) : undefined);
 await h.window.FrameSeekPersistence.ask('find red hat');
 assert.equal(h.calls.filter(c=>c.url==='/api/knowledge-chat/supplement').length,1);
 assert.equal(h.calls.filter(c=>c.url==='/api/index-chat').length,1);assert.equal(h.answers.length,1);
});
test('cloud supplement shows its request upper bound while running',async()=>{
 const h=await harness(),started=deferred();h.get('chat-engine').value='auto';
 h.get('question').value='find red hat';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'supplement_required',engine:'index',estimated_new_calls:4})
  : c.url==='/api/knowledge-chat/supplement' ? started.promise : undefined);
 const pending=h.window.FrameSeekPersistence.ask('find red hat');await tick();
 assert.match(h.get('notice').textContent,/at most 4 Cloud requests/);
 started.resolve(response({state:'waiting_budget'}));await pending;
 assert.equal(h.get('question').value,'find red hat');
});
test('an inconclusive supplement does not automatically repeat and preserves the draft',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='find red hat';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'supplement_required',engine:'index'})
  : c.url==='/api/knowledge-chat/supplement' ? response({state:'done'}) : undefined);
 await h.window.FrameSeekPersistence.ask('find red hat');
 assert.equal(h.calls.filter(c=>c.url==='/api/knowledge-chat/supplement').length,1);
 assert.equal(h.calls.some(c=>c.url==='/api/index-chat'),false);assert.equal(h.get('question').value,'find red hat');
});

test('a supplement budget wait preserves the draft and explains the cause',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='find red hat';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'supplement_required',engine:'index'})
  : c.url==='/api/knowledge-chat/supplement' ? response({state:'waiting_budget'}) : undefined);
 await h.window.FrameSeekPersistence.ask('find red hat');
 assert.match(h.get('notice').textContent,/needs budget approval/);
 assert.equal(h.get('question').value,'find red hat');
 assert.equal(h.calls.filter(c=>c.url==='/api/knowledge-chat/supplement').length,1);
 assert.equal(h.calls.some(c=>c.url==='/api/index-chat'),false);
});

test('failed supplement returns already indexed evidence without retrying the model',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='find red hat';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'supplement_required',engine:'index',has_index_matches:true})
  : c.url==='/api/knowledge-chat/supplement' ? response({state:'unknown'})
  : c.url==='/api/index-chat' ? response({...answer(c.body.request_id),engine:'index'}) : undefined);
 await h.window.FrameSeekPersistence.ask('find red hat');
 assert.equal(h.calls.filter(c=>c.url==='/api/knowledge-chat/supplement').length,1);
 assert.equal(h.calls.filter(c=>c.url==='/api/index-chat').length,1);
 assert.equal(h.answers.length,1);assert.match(h.get('notice').textContent,/unknown.*existing index/);
});

test('budget-blocked supplement can show existing indexed evidence',async()=>{
 const h=await harness();h.get('chat-engine').value='auto';h.get('question').value='find red hat';
 h.setOverride(c=>c.url==='/api/auto-chat/prepare' ? response({kind:'budget_blocked',engine:'index',has_index_matches:true,message:'补查未通过预算检查。'})
  : c.url==='/api/index-chat' ? response({...answer(c.body.request_id),engine:'index'}) : undefined);
 await h.window.FrameSeekPersistence.ask('find red hat');
 assert.equal(h.calls.filter(c=>c.url==='/api/knowledge-chat/supplement').length,0);
 assert.equal(h.answers.length,1);assert.match(h.get('notice').textContent,/预算检查.*Existing index/);
});
