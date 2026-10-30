const test = require("node:test");
const assert = require("node:assert/strict");
const { resolve } = require("../src/frameseek/static/demo-conversation.js");
const { composerSuggestions } = require("../src/frameseek/static/workbench-model.js");
const queries = [
  { id: "center", question: "包裹什么时候出现在中央？" },
  { id: "right", question: "什么时候移到右侧？" },
  { id: "person", question: "能确认是谁搬动的吗？" },
  { id: "forklift", question: "有叉车出现吗？" },
];
test("every suggested synthetic question resolves to its preset without context", () => {
  const suggestions = composerSuggestions({sourceInfo:{id:"demo_parcel"},records:[],local:false});
  assert.deepEqual(suggestions.items.map(question => resolve(question, queries, null).query?.id),
    ["center", "right", "forklift"]);
});
test("完整问题不依赖上下文", () => {
  assert.deepEqual(resolve("包裹什么时候出现在中央?", queries, null), {
    query: queries[0],
    usedContext: false,
  });
});
test("合成演示接受明确同义问法，不把其他问题当成示例", () => {
  for (const question of ["包裹何时在中央？", "包裹何时出现在中央？", "When is the parcel in the center?"]) {
    assert.equal(resolve(question, queries, null).query.id, "center");
  }
  for (const question of ["包裹没有在中央吗？", "包裹何时在右侧？", "when is the person in the center?"]) {
    assert.equal(resolve(question, queries, null).query, null);
  }
});
test("连续的指代追问承接包裹，再确认搬运者", () => {
  const first = resolve("包裹什么时候出现在中央？", queries, null);
  const second = resolve("那后来呢？", queries, first.query.id);
  const third = resolve("谁搬的？", queries, second.query.id);
  assert.equal(second.query.id, "right");
  assert.equal(third.query.id, "person");
  assert.equal(second.usedContext, true);
  assert.equal(third.usedContext, true);
});
test("English next-step fixture requires its actual center context", () => {
  assert.deepEqual(resolve("What happens next?", queries, "center"), {
    query: queries[1], usedContext: true,
  });
  assert.deepEqual(resolve("When is the parcel on the right?", queries, null), {
    query: queries[1], usedContext: false,
  });
  for (const context of [null, "right", "person", "forklift", "failure"]) {
    assert.equal(resolve("What happens next?", queries, context).query, null);
  }
  assert.equal(resolve("What happens next?", queries.filter(q => q.id !== "right"), "center").usedContext, false);
});
test("查看证据引用上一轮而不产生新的事件", () => {
  assert.equal(resolve("看看证据", queries, "center").query, queries[0]);
});
test("重置或改变话题后不沿用旧包裹上下文", () => {
  for (const id of [null, "forklift", "failure"]) {
    assert.equal(resolve("那后来呢？", queries, id).query, null);
  }
});
test("未实现的问题不伪造回答，也不改动问题或目录", () => {
  const question = "那它是送给谁的？";
  assert.equal(resolve(question, queries, "center").query, null);
  assert.equal(question, "那它是送给谁的？");
  assert.equal(queries.length, 4);
});

const { materialize } = require("../src/frameseek/static/demo-conversation.js");
const scopedQueries = [
  {
    id: "left",
    question: "包裹最初在哪里？",
    followup_map: { "那后来呢？": "center" },
  },
  {
    id: "center",
    question: "包裹什么时候出现在中央？",
    followup_map: { "那后来呢？": "right" },
  },
  { id: "right", question: "什么时候移到右侧？" },
  {
    id: "objects",
    question: "视频里有哪些固定物体？",
    followup_map: { "灯光呢？": "lighting" },
  },
  { id: "lighting", question: "灯光状态有什么变化？" },
];
test("同一短追问按所选卡片解析，跨主题不会错误承接", () => {
  assert.equal(resolve("那后来呢？", scopedQueries, "left").query.id, "center");
  assert.equal(
    resolve("那后来呢？", scopedQueries, "center").query.id,
    "right",
  );
  assert.equal(
    resolve("灯光呢？", scopedQueries, "objects").query.id,
    "lighting",
  );
  assert.equal(resolve("灯光呢？", scopedQueries, "center").query, null);
  assert.equal(resolve("灯光呢？", scopedQueries, null).query, null);
  assert.equal(
    resolve("看看证据", scopedQueries, "objects").query.id,
    "objects",
  );
});
test("演示分支保留显式父轮次，不修改原目录", () => {
  const scenario = {
    steps: [
      { query_id: "objects", parent: null },
      { query_id: "lighting", question: "灯光呢？", parent: 0 },
      { query_id: "left", parent: null },
    ],
  };
  const before = JSON.stringify({ scenario, scopedQueries });
  const seeds = materialize(scenario, scopedQueries);
  assert.deepEqual(
    seeds.map((s) => s.parentIndex),
    [null, 0, null],
  );
  assert.deepEqual(
    seeds.map((s) => s.usedContext),
    [false, true, false],
  );
  assert.equal(seeds[1].question, "灯光呢？");
  assert.equal(JSON.stringify({ scenario, scopedQueries }), before);
});
test("损坏或循环的演示引用在载入前拒绝", () => {
  for (const steps of [
    [],
    [{ query_id: "missing", parent: null }],
    [{ query_id: "objects", parent: 0 }],
    [
      { query_id: "objects", parent: null },
      { query_id: "lighting", parent: 2 },
    ],
    Array(13).fill({ query_id: "objects", parent: null }),
  ]) {
    assert.throws(() => materialize({ steps }, scopedQueries));
  }
});

test("basketball follow-ups retain the selected parent and unsupported questions stay unresolved", () => {
  const pass = {id:"pass", question:"What is visible in the pass scene?", followup_map:{"Where is the defender in the shot scene?":"shot"}};
  const shot = {id:"shot", question:"Where is the defender in the shot scene?", followup_map:{"Does the shot score?":"outcome"}};
  const outcome = {id:"outcome", question:"Does the shot score?"};
  const items=[pass,shot,outcome];
  assert.deepEqual(resolve(shot.question,items,"pass"),{query:shot,usedContext:true});
  assert.deepEqual(resolve(outcome.question,items,"shot"),{query:outcome,usedContext:true});
  assert.equal(resolve("Who is that player?",items,"shot").query,null);
});

test("legacy display translation never alters the stored fixture or a real answer", () => {
  const {displayText}=require('../src/frameseek/static/workbench-model.js');
  const query={id:'center',question:'包裹什么时候出现在中央？',video_id:'demo_parcel',response:{answer:'包裹在 00:05–00:11 出现在工作台中央。'}};
  const before=JSON.stringify(query);
  assert.equal(displayText(query.response.answer,query),'The parcel appears in the center from 00:05 to 00:11.');
  assert.equal(displayText(query.response.answer,{...query,mode:'real'}),query.response.answer);
  assert.equal(displayText(query.response.answer,{...query,video_id:'private_video'}),query.response.answer);
  assert.equal(JSON.stringify(query),before);
});
