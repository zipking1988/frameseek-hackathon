const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createStore,
  visiblePins,
} = require("../src/frameseek/static/workbench-model.js");
const frames = [
  { uri: "/media/demo_parcel/frames/0.jpg", t: 0, caption: "包裹位于左侧" },
  { uri: "/media/demo_parcel/frames/13.jpg", t: 13, caption: "包裹位于右侧" },
];
const query = {
  id: "compare",
  question: "对比前后位置",
  response: {
    status: "partial",
    answer: "可确认位置，不能确认连续移动。",
    unresolved: ["搬运者未知"],
    events: [{ evidence: frames }],
  },
};
test("订阅使用稳定快照，可退订；独立切换旧轮次而非最新问题", () => {
  const s = createStore();
  const snapshot = s.getSnapshot();
  assert.equal(s.getSnapshot(), snapshot);
  let calls = 0;
  const off = s.subscribe(() => calls++);
  s.record("one", query);
  s.record("two", { ...query, id: "other" });
  s.select("one");
  assert.equal(s.getSnapshot().selectedId, "one");
  assert.equal(calls, 3);
  off();
  s.rename("调查");
  assert.equal(calls, 3);
});
test("证据去重保留真实引用，拒绝伪造时间或其他文件", () => {
  const s = createStore();
  const first = s.pinFrame("one", query, frames[0]);
  assert.ok(first);
  assert.equal(s.pinFrame("two", query, frames[0]), first);
  assert.equal(s.getSnapshot().pins.length, 1);
  assert.equal(s.pinFrame("one", query, { ...frames[0], t: 11 }), null);
  assert.equal(
    s.pinFrame("one", query, { ...frames[0], uri: "file:///private.mp4" }),
    null,
  );
});
test("partial结论与未确认项一并导出，不保存失败或无结果为事实", () => {
  const s = createStore();
  s.rename("位置调查");
  s.group("前后核对");
  s.annotate("下一步核实搬运者");
  s.record("one", query);
  s.pinFrame("one", query, frames[1]);
  s.pinConclusion("one", query);
  const md = s.markdown();
  assert.match(md, /位置调查/);
  assert.match(md, /前后核对/);
  assert.match(md, /00:13/);
  assert.match(md, /Unconfirmed: 搬运者未知/);
  assert.match(md, /Preset results/);
  assert.match(md, /下一步核实搬运者/);
  assert.equal(
    s.pinConclusion("one", {
      ...query,
      response: { ...query.response, status: "error" },
    }),
    null,
  );
});
test("集合限制、移除与重置不遗留引用，本地文件禁止套用演示事实", () => {
  const s = createStore();
  for (let i = 0; i < 8; i++)
    assert.ok(s.pinConclusion("one", { ...query, id: String(i) }));
  assert.equal(s.pinFrame("one", query, frames[0]), null);
  s.remove(s.getSnapshot().pins[0].id);
  assert.ok(s.pinFrame("one", query, frames[0]));
  s.source(true);
  s.clear();
  assert.equal(s.getSnapshot().local, true);
  assert.deepEqual(s.getSnapshot().pins, []);
  assert.equal(s.pinFrame("one", query, frames[0]), null);
  assert.equal(s.pinConclusion("one", query), null);
  assert.match(s.markdown(), /Local video/);
  assert.doesNotMatch(s.markdown(), /Synthetic demo/);
});
test("超过十二轮仍保留原聊天及发现来源快照", () => {
  const s = createStore();
  s.record("old", query);
  s.pinConclusion("old", query);
  for (let i = 0; i < 13; i++) s.record("turn-" + i, query);
  assert.equal(s.getSnapshot().records.length, 14);
  assert.equal(
    s.getSnapshot().records.some((r) => r.id === "old"),
    true,
  );
  assert.equal(s.getSnapshot().pins[0].turnId, "old");
  assert.equal(
    s.getSnapshot().pins[0].query.response.unresolved[0],
    "搬运者未知",
  );
});

test("菜单与检查器共享面板选择，切换不改写调查或草稿数据", () => {
  const s = createStore();
  s.record("one", query);
  s.pinFrame("one", query, frames[0]);
  s.annotate("核实连续性");
  const previous = s.getSnapshot();
  s.showPanel("findings");
  assert.equal(s.getSnapshot().inspectorTab, "findings");
  assert.equal(s.getSnapshot().records, previous.records);
  assert.equal(s.getSnapshot().pins, previous.pins);
  assert.equal(s.getSnapshot().selectedId, "one");
  assert.equal(s.getSnapshot().note, "核实连续性");
  const valid = s.getSnapshot();
  s.showPanel("unknown");
  assert.equal(s.getSnapshot(), valid);
  s.clear();
  assert.equal(s.getSnapshot().inspectorTab, "context");
});

test("发现筛选与时间排序保持来源及保存顺序，不修改原集合", () => {
  const s = createStore();
  const later = s.pinFrame("one", query, frames[1]);
  const conclusion = s.pinConclusion("one", query);
  const earlier = s.pinFrame("one", query, frames[0]);
  const original = s.getSnapshot().pins;
  assert.deepEqual(visiblePins(original, "evidence", "time"), [earlier, later]);
  assert.deepEqual(visiblePins(original, "conclusion", "time"), [conclusion]);
  assert.deepEqual(visiblePins(original, "all", "time"), [
    earlier,
    later,
    conclusion,
  ]);
  assert.deepEqual(visiblePins(original, "all", "saved"), [
    later,
    conclusion,
    earlier,
  ]);
  assert.deepEqual(original, [later, conclusion, earlier]);
  assert.equal(visiblePins(original, "evidence", "time")[0].query, query);
  assert.deepEqual(visiblePins([], "evidence", "time"), []);
});

test("history capacity rejects a new card without evicting old questions",()=>{
  const store=createStore();
  for(let i=1;i<=100;i++) assert.equal(store.record(`task-${i}`,{...query,id:`q${i}`}),true);
  const before=store.getSnapshot();
  assert.equal(store.record('task-101',{...query,id:'overflow'}),false);
  assert.equal(store.getSnapshot(),before);
  assert.equal(store.getSnapshot().records[0].id,'task-1');
  store.hydrate(before);
  assert.equal(store.getSnapshot().records.length,100);
});
