const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createModel,
} = require("../src/frameseek/static/investigation-canvas.js");
test("a branch attaches to the explicitly selected turn, not the latest turn", () => {
  const m = createModel();
  m.add("center", null);
  m.add("right", "center");
  m.add("person", "right");
  const branch = m.add("revisit", "center");
  assert.equal(branch.parent, "center");
  assert.equal(m.items.get("person").parent, "right");
  assert.ok(branch.y > m.items.get("right").y);
});
test("zoom preserves the world point under the pointer and clamps scale", () => {
  const m = createModel();
  m.pan(110, -60);
  const before = m.view;
  const point = {
    x: (400 - before.x) / before.scale,
    y: (300 - before.y) / before.scale,
  };
  m.zoom(1.4, 400, 300);
  const after = m.view;
  assert.equal(point.x * after.scale + after.x, 400);
  assert.equal(point.y * after.scale + after.y, 300);
  m.zoom(100, 400, 300);
  assert.equal(m.view.scale, 1.6);
  m.zoom(-3, 400, 300);
  assert.equal(m.view.scale, 0.1);
  m.move("missing", NaN, 4);
  m.pan(Infinity, 2);
  assert.ok(Number.isFinite(m.view.x));
});
test("time and status sorting preserve graph parents and manually moved nodes are restored by arrangement", () => {
  const m = createModel();
  m.add("right", null, { time: 11, status: "ok" });
  m.add("center", "right", { time: 5, status: "ok" });
  m.add("failure", null, { status: "error" });
  m.move("center", -4000, 9000);
  m.arrange("time");
  assert.ok(m.items.get("center").x < m.items.get("right").x);
  assert.equal(m.items.get("center").parent, "right");
  assert.ok(m.items.get("center").y < 9000);
  m.arrange("status");
  assert.ok(m.items.get("failure").x > m.items.get("center").x);
});
test("fit includes nodes in negative world space and reset removes stale parents and transforms", () => {
  const m = createModel();
  m.add("a", null);
  m.add("b", "a");
  m.move("a", -600, -350);
  m.move("b", 800, 400);
  m.fit(1440, 1058);
  const v = m.view;
  for (const i of m.items.values()) {
    assert.ok(i.x * v.scale + v.x >= 0);
    assert.ok((i.x + i.width) * v.scale + v.x <= 1440);
    assert.ok(i.y * v.scale + v.y >= 0);
    assert.ok((i.y + i.height) * v.scale + v.y <= 1058 - 100);
  }
  m.remove("a");
  assert.equal(m.items.get("b").parent, null);
  m.clear();
  assert.equal(m.items.size, 0);
  assert.deepEqual(m.view, { x: 0, y: 0, scale: 1 });
});

test("fit keeps a twelve-turn investigation chain within the viewport", () => {
  const m = createModel();
  for (let n = 0; n < 12; n++) m.add(String(n), n ? String(n - 1) : null);
  m.fit(1376, 998);
  const v = m.view;
  for (const i of m.items.values()) {
    assert.ok(i.x * v.scale + v.x >= 0);
    assert.ok((i.x + i.width) * v.scale + v.x <= 1376);
  }
});

test("横向评审排列保留分支且在小桌面保有可读比例", () => {
  const m = createModel();
  m.add("root", null);
  m.add("tape", "root");
  m.add("lighting", "root");
  for (const item of m.items.values()) item.height = 530;
  m.arrange("spread");
  m.fit(1136, 740);
  assert.ok(m.view.scale >= 0.7);
  for (const item of m.items.values()) {
    assert.ok((item.y + item.height) * m.view.scale + m.view.y <= 505);
    assert.ok(item.x * m.view.scale + m.view.x >= 0);
    assert.ok((item.x + item.width) * m.view.scale + m.view.x <= 1136);
  }
  assert.equal(m.items.get("tape").parent, "root");
  assert.equal(m.items.get("lighting").parent, "root");
  assert.ok(m.items.get("tape").x < m.items.get("lighting").x);
});

test("工作台按对象排列分开问题和发现，保留证据来源", () => {
  const m = createModel();
  m.add("q1", null);
  m.add("q2", "q1");
  m.add("frame", "q1", { kind: "evidence" });
  m.add("claim", "q2", { kind: "conclusion" });
  m.arrange("workspace");
  assert.ok(
    m.items.get("frame").y > m.items.get("q2").y + m.items.get("q2").height,
  );
  assert.equal(m.items.get("frame").parent, "q1");
  assert.equal(m.items.get("claim").parent, "q2");
});
test("工作台取景按输入区留白，发现卡片不被底部遮挡", () => {
  const m = createModel();
  for (let n = 0; n < 6; n++)
    m.add(String(n), n ? String(n - 1) : null, {
      kind: n >= 3 ? "evidence" : null,
    });
  m.arrange("workspace");
  m.fit(1504, 1228, { top: 100, bottom: 240 });
  for (const item of m.items.values())
    assert.ok((item.y + item.height) * m.view.scale + m.view.y <= 988.001);
});

test("对齐吸附选最近轴线，超出容差保持自由移动且不改父关系", () => {
  const m = createModel();
  m.add("a", null);
  m.add("b", "a");
  m.move("a", 100, 200);
  const near = m.snap("b", 106, 195, 7);
  assert.equal(near.x, 100);
  assert.equal(near.y, 200);
  m.move("b", near.x, near.y);
  assert.equal(m.items.get("b").parent, "a");
  const far = m.snap("b", 120, 224, 7);
  assert.equal(far.x, 120);
  assert.equal(far.y, 224);
  assert.deepEqual(far.guides, []);
});

test("选中分支关联祖先和下游发现，其他分支不误标为来源", () => {
  const m = createModel();
  m.add("root", null);
  m.add("left", "root");
  m.add("right", "root");
  m.add("frame", "left", { kind: "evidence" });
  assert.equal(m.related(null).size, 0);
  assert.deepEqual([...m.related("left")].sort(), ["frame", "left", "root"]);
  assert.deepEqual([...m.related("frame")].sort(), ["frame", "left", "root"]);
  m.remove("left");
  assert.deepEqual([...m.related("frame")], ["frame"]);
});

test("小地图包含负坐标对象与远处视野，投影随缩放平移更新", () => {
  const m = createModel();
  m.add("a", null);
  m.add("b", "a");
  m.move("a", -800, -900);
  m.move("b", 800, 600);
  m.pan(-3000, 400);
  m.zoom(0.4, 320, 200);
  const map = m.overview(1000, 700);
  for (const rect of [...map.nodes, map.viewport]) {
    assert.ok(rect.x >= 0 && rect.y >= 0);
    assert.ok(rect.x + rect.width <= 184.001);
    assert.ok(rect.y + rect.height <= 106.001);
  }
  assert.equal(map.nodes.length, 2);
  const ratio = map.viewport.width / map.viewport.height;
  assert.ok(Math.abs(ratio - 1000 / 700) < 0.0001);
  m.clear();
  assert.equal(m.overview(1000, 700).nodes.length, 0);
});

test("折叠只隐藏实际后代，其他分支与坐标保持，展开恢复原对象", () => {
  const m = createModel();
  m.add("root", null);
  m.add("a", "root");
  m.add("b", "root");
  m.add("frame", "a", { kind: "evidence" });
  m.move("frame", -450, 900);
  m.fold("a", true);
  assert.deepEqual(
    m.visible().map((i) => i.id),
    ["root", "a", "b"],
  );
  assert.equal(m.items.size, 4);
  assert.equal(m.items.get("frame").parent, "a");
  m.fold("a", false);
  assert.equal(m.visible().length, 4);
  assert.equal(m.items.get("frame").x, -450);
  assert.equal(m.items.get("frame").y, 900);
});

test("选择隐藏轮次展开祖先，保留独立子路径的折叠状态", () => {
  const m = createModel();
  m.add("root", null);
  m.add("a", "root");
  m.add("b", "root");
  m.add("frame", "a", { kind: "evidence" });
  m.add("claim", "b", { kind: "conclusion" });
  m.fold("a", true);
  m.fold("b", true);
  m.fold("root", true);
  m.expandPath("frame");
  assert.deepEqual([...m.collapsed], ["b"]);
  assert.ok(m.visible().some((i) => i.id === "frame"));
  assert.ok(!m.visible().some((i) => i.id === "claim"));
});

test("隐藏对象不影响小地图与适应视图，展开后重新纳入取景", () => {
  const m = createModel();
  m.add("root", null);
  m.add("far", "root");
  m.move("far", 9000, 9000);
  m.fold("root", true);
  m.fit(1000, 800);
  assert.equal(m.view.scale, 1);
  assert.equal(m.overview(1000, 800).nodes.length, 1);
  m.fold("root", false);
  m.fit(1000, 800);
  assert.ok(m.view.scale < 1);
  assert.equal(m.overview(1000, 800).nodes.length, 2);
});

test("路径命名不覆盖原始问题，移除父节点释放隐藏子项，重置清除折叠", () => {
  const m = createModel();
  m.add("root", null, { question: "原始问题" });
  m.add("child", "root");
  m.renameBranch("root", "  位置变化  ");
  m.fold("root", true);
  assert.equal(m.items.get("root").question, "原始问题");
  assert.equal(m.items.get("root").branchName, "位置变化");
  m.remove("root");
  assert.equal(m.visible()[0].id, "child");
  assert.equal(m.collapsed.size, 0);
  m.clear();
  assert.equal(m.visible().length, 0);
});

test("摘要阈值带缓冲区，边界缩放不会反复切换内容", () => {
  const {
    densityFor,
  } = require("../src/frameseek/static/investigation-canvas.js");
  assert.equal(densityFor(0.7, "detail"), "overview");
  assert.equal(densityFor(0.76, "overview"), "overview");
  assert.equal(densityFor(0.81, "overview"), "overview");
  assert.equal(densityFor(0.85, "overview"), "detail");
  assert.equal(densityFor(0.76, "detail"), "detail");
});

test("极小取景使用图谱，返回摘要时保留阈值缓冲", () => {
  const {
    densityFor,
  } = require("../src/frameseek/static/investigation-canvas.js");
  assert.equal(densityFor(0.3, "detail"), "map");
  assert.equal(densityFor(0.41, "map"), "map");
  assert.equal(densityFor(0.45, "map"), "overview");
  assert.equal(densityFor(0.9, "map"), "detail");
});

test("折叠路径作为整体移动，其他路径不受影响", () => {
  const m = createModel();
  const root = m.add("root", null);
  const child = m.add("child", "root");
  const frame = m.add("frame", "child", { kind: "evidence" });
  const other = m.add("other", null);
  const start = {
    root: { x: root.x, y: root.y },
    child: { x: child.x, y: child.y },
    frame: { x: frame.x, y: frame.y },
    other: { x: other.x, y: other.y },
  };
  m.fold("root", true);
  m.move("root", root.x + 120, root.y - 70);
  m.fold("root", false);
  assert.equal(child.x, start.child.x + 120);
  assert.equal(frame.y, start.frame.y - 70);
  assert.equal(other.x, start.other.x);
  assert.equal(other.y, start.other.y);
  assert.equal(frame.parent, "child");
});

test("折叠时整理压缩空位，展开后重排避免重叠且保留关系", () => {
  const m = createModel();
  m.add("root", null);
  m.add("child", "root");
  m.add("frame", "child", { kind: "evidence" });
  m.add("other", null);
  m.fold("root", true);
  m.arrange("workspace");
  assert.equal(m.items.get("other").x, 504);
  m.expandAll();
  const rectangles = m.visible();
  for (let i = 0; i < rectangles.length; i++)
    for (let j = i + 1; j < rectangles.length; j++) {
      const a = rectangles[i],
        b = rectangles[j];
      assert.ok(
        a.x + a.width <= b.x ||
          b.x + b.width <= a.x ||
          a.y + a.height <= b.y ||
          b.y + b.height <= a.y,
      );
    }
  assert.equal(m.items.get("child").parent, "root");
  assert.equal(m.items.get("frame").parent, "child");
});

test("空间快照恢复位置、视角、路径名称与折叠，丢失对象不复活", () => {
  const original = createModel();
  original.add("root", null); original.add("child", "root");
  original.renameBranch("root", "物体位置"); original.move("root", 620, 400); original.fold("root", true);
  const saved = original.snapshot();
  const restored = createModel(); restored.add("root", null); restored.add("child", "root");
  restored.restore(saved);
  assert.equal(restored.items.get("root").x, 620);
  assert.equal(restored.items.get("root").branchName, "物体位置");
  assert.deepEqual([...restored.collapsed], ["root"]);
  assert.deepEqual(restored.view, original.view);
  restored.remove("child"); restored.restore(saved);
  assert.equal(restored.items.has("child"), false);
});

test('one batch translation moves a folded subtree once even if a child is also selected',()=>{
 const m=createModel();m.add('a',null);m.add('b','a');m.add('c','b');m.fold('a',true);
 const before=m.snapshot();m.moveMany(['a','b'],20,-10);
 for(const n of before.nodes){assert.equal(m.items.get(n.id).x,n.x+20);assert.equal(m.items.get(n.id).y,n.y-10);}
});
test('undo/redo restores groups, names and positions without reverting view, new answers or parents',()=>{
 const m=createModel();m.add('a',null);m.add('b','a');const x=m.items.get('a').x;
 m.perform('分组',()=>{m.group(['a','b'],'调查线索','g1');m.renameBranch('a','位置');m.moveMany(['a','b'],100,30);});
 m.add('new','b');m.pan(18,25);const view=m.view;
 assert.equal(m.undo(),'分组');assert.equal(m.groups.size,0);assert.equal(m.items.get('a').branchName,'');assert.equal(m.items.get('a').x,x);assert.equal(m.items.get('new').parent,'b');assert.deepEqual(m.view,view);
 assert.equal(m.redo(),'分组');assert.equal(m.groups.get('g1').name,'调查线索');assert.equal(m.items.get('a').x,x+100);
 m.undo();m.perform('另一次移动',()=>m.moveMany(['a'],10,0));assert.equal(m.canRedo,false);
});
test('group hydration filters missing nodes; deletion and clear cannot resurrect evidence objects',()=>{
 const m=createModel();m.add('a',null);m.add('b',null);m.add('c',null);
 m.perform('分组',()=>m.group(['a','b','c'],'核对','g1'));const saved=m.snapshot();m.remove('b');m.undo();m.redo();assert.deepEqual(m.groups.get('g1').members,['a','c']);
 m.restore({...saved,groups:[{id:'g2',name:'恢复',members:['a','a','missing','c']}]});assert.deepEqual(m.groups.get('g2').members,['a','c']);
 m.clear();assert.equal(m.groups.size,0);assert.equal(m.canUndo,false);assert.equal(m.canRedo,false);
});
test('alignment and spacing preserve evidence parents; history is bounded and no-op does not add entries',()=>{
 const m=createModel();m.add('a',null);m.add('b','a');m.add('c','a');
 m.perform('对齐',()=>m.align(['a','b','c'],'left'));assert.equal(m.items.get('a').x,m.items.get('b').x);assert.equal(m.items.get('c').parent,'a');
 assert.equal(m.perform('不移动',()=>m.moveMany(['a'],0,0)),false);
 for(let i=0;i<70;i++)m.perform('移动',()=>m.moveMany(['a'],1,0));let count=0;while(m.undo())count++;assert.equal(count,60);
});


test('Shift removing a nonprimary selection keeps follow-up anchored to the remaining checked card',()=>{
 const fs=require('node:fs'),vm=require('node:vm');
 const source=fs.readFileSync(require.resolve('../src/frameseek/static/investigation-canvas.js'),'utf8');
 const chooser=source.slice(source.indexOf('    function choose('),source.indexOf('    function clearScope('));
 const selectedIds=new Set(),elements=new Map();
 const $=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);};
 const records=new Map(['A','B'].map(id=>[id,{query:{id,question:id,response:{id}},select:{setAttribute(){}}}]));
 const ctx=vm.createContext({records,selectedIds,$,model:{expandPath(){}},detailFocus:null,selected:null,callbacks:{select(){}},draw(){},clearScope(){}});
 vm.runInContext(chooser,ctx);vm.runInContext("choose('A');choose('B',false,true);choose('A',false,true)",ctx);
 assert.equal(ctx.selected,'B');assert.equal(elements.get('composer-label').textContent,'Following up on: B');assert.deepEqual([...selectedIds],['B']);
});


test('aligning a folded parent ignores selected hidden children and preserves their relative positions',()=>{
 const m=createModel();m.add('parent',null);m.add('child','parent');m.add('other',null);
 m.move('parent',100,0);m.move('child',500,20);m.move('other',0,100);m.fold('parent',true);
 m.align(['child','parent','other'],'left');assert.equal(m.items.get('parent').x,0);assert.equal(m.items.get('child').x,400);assert.equal(m.items.get('other').x,0);
});


test('holding Shift before title drag preserves selection and still begins a free-placement gesture',()=>{
 const fs=require('node:fs'),vm=require('node:vm');
 const source=fs.readFileSync(require.resolve('../src/frameseek/static/investigation-canvas.js'),'utf8');
 const chooser=source.slice(source.indexOf('    function choose('),source.indexOf('    function clearScope('));
 const begin=source.indexOf('      drag.addEventListener("pointerdown", (e) => {');
 const end=source.indexOf('      const end = () => {',begin);
 let handler;const selectedIds=new Set(),elements=new Map();
 const $=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);};
 const records=new Map(['A','B'].map(id=>[id,{query:{id,question:id,response:{id}},select:{setAttribute(){}}}]));
 const ctx=vm.createContext({records,selectedIds,$,model:{expandPath(){},items:new Map([['A',{x:1,y:2}]]),view:{scale:1},checkpoint:()=>({})},detailFocus:null,selected:null,gesture:null,callbacks:{select(){}},draw(){},clearScope(){selectedIds.clear();},turn:{id:'A',classList:{add(){}}},drag:{addEventListener:(name,fn)=>handler=fn,focus(){},setPointerCapture(){}}});
 vm.runInContext(chooser,ctx);vm.runInContext("choose('A');choose('B',false,true)",ctx);
 vm.runInContext(source.slice(begin,end),ctx);
 handler({button:0,shiftKey:true,pointerId:1,clientX:10,clientY:20,preventDefault(){}});
 assert.deepEqual([...selectedIds],['A','B']);assert.equal(ctx.selected,'A');assert.equal(ctx.gesture.kind,'card');assert.deepEqual(Array.from(ctx.gesture.ids),['A','B']);
});

test('failed real turns remain selectable but cannot anchor new queries; verified partial results can',()=>{
 const fs=require('node:fs'),vm=require('node:vm');
 const source=fs.readFileSync(require.resolve('../src/frameseek/static/investigation-canvas.js'),'utf8');
 const chooser=source.slice(source.indexOf('    function choose('),source.indexOf('    function clearScope('));
 const context=source.slice(source.indexOf('      context: () => {'),source.indexOf('      reveal,',source.indexOf('      context: () => {'))).trim().replace(/^context: /,'').replace(/,$/,'');
 const selectedIds=new Set(),elements=new Map();
 const $=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);};
 const records=new Map(['failed','partial','empty'].map(id=>[id,{query:{mode:'real',id,question:id,response:{status:id==='failed'?'error':'partial',events:id==='partial'?[{evidence:[{t:326.1}]}]:[]}},select:{setAttribute(){}}}]));
 const ctx=vm.createContext({records,selectedIds,$,model:{expandPath(){}},detailFocus:null,selected:null,callbacks:{select(){}},draw(){},clearScope(){}});
 vm.runInContext(chooser+'\nconst currentContext = '+context,ctx);
 for(const id of ['failed','empty']) {
   vm.runInContext(`choose('${id}')`,ctx);
   assert.equal(ctx.selected,id);
   assert.equal(vm.runInContext('currentContext()',ctx),null);
   assert.match(elements.get('composer-label').textContent,/Ask again about/);
 }
 vm.runInContext("choose('partial')",ctx);
 assert.equal(vm.runInContext('currentContext().query.id',ctx),'partial');
 assert.equal(elements.get('composer-label').textContent,'Following up on: partial');
});

test('card reply shortcut passes its originating turn to the fill-only composer callback',()=>{
 const fs=require('node:fs'),vm=require('node:vm');
 const source=fs.readFileSync(require.resolve('../src/frameseek/static/investigation-canvas.js'),'utf8');
 const start=source.indexOf('      (query.kind ? [] : replies).slice(0, 2).forEach');
 const end=source.indexOf('      if (!query.kind) actions.append(follow);',start);
 const buttons=[],calls=[];
 const ctx=vm.createContext({query:{},display:value=>value,replies:['那后来呢？'],turn:{id:'task-A'},
  document:{createElement:()=>({addEventListener:(name,callback)=>buttons.push(callback)})},
  callbacks:{ask:(...args)=>calls.push(args)},actions:{append(){}}});
 vm.runInContext(source.slice(start,end),ctx);
 assert.equal(calls.length,0);buttons[0]();
 assert.deepEqual(calls,[['那后来呢？','task-A']]);
});
