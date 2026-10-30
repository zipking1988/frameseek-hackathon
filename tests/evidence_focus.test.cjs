/* Behavioral regression coverage without a browser dependency or network access. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

let activeDocument;
class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.isConnected = true;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.value = "";
    this.hidden = false;
    this.scrollTop = 0;
    this.textContent = "";
    this.firstChild = { textContent: "" };
    this.classes = new Set();
    this.classList = {
      add: (x) => this.classes.add(x),
      remove: (x) => this.classes.delete(x),
      contains: (x) => this.classes.has(x),
      toggle: (x, enabled) => {
        if (enabled) this.classes.add(x);
        else this.classes.delete(x);
      },
    };
  }
  set className(value) {
    this.classes = new Set(value.split(" "));
  }
  get className() {
    return [...this.classes].join(" ");
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  prepend(node) {
    node.parent = this;
    this.children.unshift(node);
  }
  replaceChildren() {
    this.children = [];
  }
  remove() {
    this.isConnected = false;
    this.parent.children = this.parent.children.filter((x) => x !== this);
  }
  get firstElementChild() {
    return this.children[0];
  }
  setAttribute(key, value) {
    this.attributes[key] = value;
  }
  getAttribute(key) {
    return this.attributes[key] ?? null;
  }
  removeAttribute(key) {
    delete this.attributes[key];
  }
  addEventListener(name, fn) {
    (this.listeners[name] ??= []).push(fn);
  }
  dispatch(name) {
    for (const fn of this.listeners[name] ?? [])
      fn({ currentTarget: this, preventDefault() {} });
  }
  dispatchEvent(event) { this.dispatch(event.type); }
  click() {
    this.dispatch("click");
  }
  focus() {
    activeDocument.activeElement = this;
    this.focused = true;
  }
  getBoundingClientRect() {
    return { top: 0 };
  }
  scrollIntoView() {}
  pause() {
    this.paused = true;
  }
  load() {}
  querySelector() {
    return new Element();
  }
}
async function harness(readyState = 1, width = 1440) {
  const ids = new Map();
  const get = (id) => {
    if (!ids.has(id)) ids.set(id, new Element());
    return ids.get(id);
  };
  const sourceSteps = [new Element(), new Element(), new Element()];
  const shell = new Element();
  const player = get("player");
  Object.assign(player, {
    readyState,
    duration: 18,
    currentTime: 0,
    paused: true,
  });
  const walk = (nodes) => nodes.flatMap((n) => [n, ...walk(n.children)]);
  const byClass = (cls) =>
    walk([...ids.values()]).filter((n) => n.classList.contains(cls));
  const document = {
    getElementById: get,
    createElement: (tag) => new Element(tag),
    querySelector: (s) =>
      s === ".source-item span"
        ? get("source-name")
        : s.startsWith("[data-turn=")
          ? byClass("history-task").find(
              (n) => n.dataset.turn === s.split('"')[1],
            )
          : s === ".agent-shell"
            ? shell
            : s.includes("li:first-child")
              ? sourceSteps[0]
              : s.includes("li:nth-child(2)")
                ? sourceSteps[1]
                : s.includes("li:last-child")
                  ? sourceSteps[2]
                  : null,
    querySelectorAll: (s) =>
      s === ".run-steps li"
        ? sourceSteps
        : s === ".history-task"
          ? byClass("history-task")
          : s === ".evidence-button"
            ? byClass("evidence-button")
            : [],
  };
  activeDocument = document;
  const context = vm.createContext({
    document,
    window: {
      FrameSeekWorkbenchModel: require('../src/frameseek/static/workbench-model.js'),
      innerWidth: width,
      matchMedia: () => ({ matches: true }),
      addEventListener() {},
      dispatchEvent() {},
    },
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: async () => ({ ok: true, json: async () => ({ queries: [] }) }),
    URL: {},
    Event,
    console,
  });
  vm.runInContext(
    fs.readFileSync(
      path.join(__dirname, "../src/frameseek/static/app.js"),
      "utf8",
    ),
    context,
  );
  await new Promise(setImmediate);
  const run = (source) => vm.runInContext(source, context);
  const answer = (status, events = []) => ({
    question: status,
    response: {
      status,
      answer: status,
      events,
      unresolved: [],
      errors: [],
      followups: [],
    },
  });
  return { get, player, run, shell, answer, document };
}

const event = { start: 5, end: 11, action: "center", summary: "parcel", evidence: [{ t: 6, uri: "/frame.jpg", caption: "parcel" }] };
function evidenceButton(h) {
  h.run("renderAnswer(demo.queries[0])");
  return h.get("messages").children[0].children.find(n => n.classList.contains("event")).children.find(n => n.classList.contains("evidence-grid")).children[0];
}
async function conversation(ready = 1, width = 375) {
  const h = await harness(ready, width);
  h.run(`demo.queries = [${JSON.stringify({ question: "center", response: { status: "ok", answer: "center", events: [event], unresolved: [], errors: [], followups: [] } })}]`);
  return h;
}
test("canvas frame and time replay do not replace the draft's reply context", async () => {
  const h = await conversation(1, 1440);
  const evidence = evidenceButton(h);
  const row = h.get("messages").children[0].children.find(n => n.classList.contains("event"));
  const timeButton = row.children.find(n => n.classList.contains("event-toolbar")).children[0];
  h.get("question").value = "draft to retain";
  h.run(`globalThis.selectedDuringReplay = [];
    investigation = { choose: id => selectedDuringReplay.push(id), evidence() {} };
    window.FrameSeekWorkbench = { select: id => selectedDuringReplay.push(id) };`);
  evidence.click();
  assert.equal(h.player.currentTime, 6);
  timeButton.click();
  assert.equal(h.player.currentTime, 5);
  assert.deepEqual([...h.run("selectedDuringReplay")], []);
  assert.equal(h.get("question").value, "draft to retain");
});
test("mobile evidence hands focus to return action, then restores clicked evidence and draft", async () => {
  const h = await conversation();
  const evidence = evidenceButton(h);
  h.get("question").value = "draft to retain";
  h.get("question").focus();
  evidence.click();
  assert.equal(h.document.activeElement, h.get("toggle-evidence"));
  assert.equal(h.player.currentTime, 6);
  h.get("toggle-evidence").click();
  assert.equal(h.document.activeElement, evidence);
  assert.equal(h.get("question").value, "draft to retain");
});
test("loading video retains original evidence trigger for focus restoration", async () => {
  const h = await conversation(0);
  const evidence = evidenceButton(h);
  evidence.click();
  h.player.readyState = 1;
  h.player.dispatch("loadedmetadata");
  assert.equal(h.document.activeElement, h.get("toggle-evidence"));
  h.get("toggle-evidence").click();
  assert.equal(h.document.activeElement, evidence);
});
test("removed evidence cannot receive focus; desktop seek preserves current focus", async () => {
  let h = await conversation();
  const evidence = evidenceButton(h);
  evidence.click();
  evidence.remove();
  h.get("toggle-evidence").click();
  assert.equal(h.document.activeElement, h.get("question"));
  h = await conversation(1, 1440);
  const desktopEvidence = evidenceButton(h);
  desktopEvidence.focus();
  desktopEvidence.click();
  assert.equal(h.document.activeElement, desktopEvidence);
  assert.equal(h.shell.classList.contains("evidence-open"), false);
});

test("unsupported follow-up on another topic gives a supported recovery question", async () => {
  const h = await conversation();
  h.get("question").value = "那后来呢？";
  h.run('lastQueryId = "forklift"; ask("那后来呢？")');
  assert.equal(h.get("question").value, "那后来呢？");
  assert.equal(h.get("messages").children.length, 0);
  assert.match(h.get("form-notice").textContent, /当前话题不支持这个追问/);
  assert.match(h.get("form-notice").textContent, /包裹什么时候出现在中央/);
  assert.doesNotMatch(h.get("form-notice").textContent, /演示支持「那后来呢/);
  h.run('lastQueryId = "center"; ask("不支持的问题")');
  assert.match(h.get("form-notice").textContent, /演示支持「那后来呢/);
});

test("uploaded source uses its own preview poster before analysis and replaces it on navigation", async () => {
  const h = await harness();
  h.run("window.FrameSeekWorkbench = { source() {}, describe() {}, clear() {} }");
  h.run('window.FrameSeekSessionUI.applySource({id:"vid_a", name:"A", job:{state:"awaiting_configuration"}, metadata:{preview:[{uri:"/media/vid_a/frames/preview_001.jpg"}]}})');
  h.get('evidence-question').textContent='上一个视频的问题';
  assert.equal(h.player.poster, "/media/vid_a/frames/preview_001.jpg");
  assert.equal(h.player.src, "/media/vid_a/video");
  assert.equal(h.get("question").disabled, true);
  h.run('window.FrameSeekSessionUI.applySource({id:"vid_b", name:"B", job:{state:"ready"}, metadata:{preview:[{uri:"/media/vid_b/frames/preview_001.jpg"}]}})');
  assert.equal(h.get('evidence-question').textContent,'素材预览');
  assert.equal(h.player.poster, "/media/vid_b/frames/preview_001.jpg");
  assert.equal(h.get("question").disabled, false);
});

test("switching from an uploaded video resets synthetic metadata before and after media load", async () => {
  const h = await harness();
  h.run("window.FrameSeekWorkbench = { source() {}, describe() {}, clear() {} }");
  h.run('window.FrameSeekSessionUI.applySource({id:"vid_long", name:"Long video", job:{state:"ready"}, metadata:{duration:384}})');
  Object.assign(h.player, {duration:384, videoWidth:1280, videoHeight:720});
  h.player.dispatch("loadedmetadata");
  assert.match(h.get("video-meta").textContent, /06:24/);
  h.run('window.FrameSeekSessionUI.applySource({id:"demo_parcel",metadata:{duration:18,audio:false}})');
  assert.equal(h.get("video-meta").textContent, "18 秒 · 1600 × 900 · 无音轨");
  Object.assign(h.player, {duration:18, videoWidth:1600, videoHeight:900});
  h.player.dispatch("loadedmetadata");
  assert.equal(h.get("video-meta").textContent, "18 秒 · 1600 × 900 · 无音轨");
});

test("retrying a failed real turn preserves the draft and explains a fresh question without dispatch", async () => {
  const h = await harness();
  h.run(`window.FrameSeekWorkbench = {getSnapshot: () => ({records: [{id: 'failed', query: {mode: 'real', question: '请查看视频05:20至05:40', response: {events: []}}}]})};
    investigation = {choose(id) { window.chosen = id; }};
    window.FrameSeekPersistence = {ask() { throw new Error('must not dispatch'); }};`);
  h.get('question').value = '继续问题4';
  h.run("window.FrameSeekUI.continueFrom('failed')");
  assert.equal(h.get('question').value, '继续问题4');
  assert.match(h.get('form-notice').textContent, /不会自动重试/);
  assert.doesNotMatch(h.get('form-notice').textContent, /请写出完整问题/);
  assert.equal(h.document.activeElement, h.get('question'));
  h.get('question').value = '';
  let changes=0; h.get('question').addEventListener('input',()=>changes++);
  h.run("window.FrameSeekUI.continueFrom('failed')");
  assert.equal(h.get('question').value, '请查看视频05:20至05:40');
  assert.equal(changes,1,'restored original question participates in draft autosave');
});
