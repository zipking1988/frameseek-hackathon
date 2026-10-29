const test = require('node:test');
const assert = require('node:assert/strict');
const {toggleTurn,panelWidth,createStore} = require('../src/frameseek/static/workbench-model.js');

test('deletion is reversible across JSON save/restore and preserves drafts, descendants and pins',()=>{
  const before={epoch:3,records:[{id:'task-4',query:{id:'paid-request-4'}},{id:'task-7',query:{id:'paid-request-7',parentTurn:'task-4'}}],pins:[{id:'finding-1',turnId:'task-4'}],selectedId:'task-4',ui:{draft:'unfinished question',playerTime:320},canvas:{nodes:[{id:'task-4',x:800,y:420},{id:'task-7',x:1200,y:450}],view:{x:4,y:7,scale:.8},selected:'task-4'}};
  const deleted=toggleTurn(before,'task-4',true);
  assert.equal(deleted.records[0].hidden,true);
  assert.equal(deleted.selectedId,null);
  assert.equal(deleted.canvas.nodes.some(n=>n.id==='task-4'),false);
  assert.deepEqual(deleted.ui,before.ui);
  assert.deepEqual(deleted.pins,before.pins);
  assert.equal(deleted.records[1].query.parentTurn,'task-4');
  assert.equal(deleted.epoch,3);
  const stored=JSON.parse(JSON.stringify(deleted));
  const store=createStore();store.hydrate(stored);store.select('task-4');
  assert.equal(store.getSnapshot().selectedId,null);
  const restored=toggleTurn(stored,'task-4',false);
  assert.equal(restored.selectedId,'task-4');
  assert.equal(restored.records[0].hidden,false);
  assert.deepEqual(restored.canvas.nodes.find(n=>n.id==='task-4'),before.canvas.nodes[0]);
  assert.deepEqual(restored.canvas.view,before.canvas.view);
  assert.equal(before.records[0].hidden,undefined);
  assert.equal(toggleTurn(before,'missing',true),null);
});

test('panel dragging is bounded on both sides and retains usable canvas width',()=>{
  assert.equal(panelWidth(100,'source',2337,344),280);
  assert.equal(panelWidth(2000,'source',2337,344),900);
  assert.equal(panelWidth(100,'inspector',2337,720),260);
  assert.equal(panelWidth(2000,'inspector',2337,720),600);
  assert.equal(panelWidth(900,'source',1440,344),620);
  assert.equal(panelWidth(600,'inspector',1440,620),344);
  assert.equal(panelWidth(NaN,'source',1440,344),280);
});

test('hidden questions are excluded from reports while explicitly saved findings keep their source',()=>{
 const {create}=require('../src/frameseek/static/investigation-report.js');
 const query={id:'q4',question:'failed',response:{status:'error',events:[],answer:'failed'}};
 const state={records:[{id:'task-4',query,hidden:true}],pins:[]};
 assert.equal(create(state).questions.length,0);
 assert.equal(create({...state,pins:[{id:'finding-1',kind:'conclusion',title:'saved',turnId:'task-4',query}]}).questions[0].retained_by_finding,true);
});

test('removed records remain recoverable after more than twelve turns without resetting IDs',()=>{
 const store=createStore();
 for(let i=1;i<=12;i++)store.record(`task-${i}`,{id:`q${i}`});
 const saved=toggleTurn(store.getSnapshot(),'task-1',true);store.hydrate(saved);
 store.record('task-13',{id:'q13'});
 assert.equal(store.getSnapshot().records.length,13);
 assert.equal(store.getSnapshot().records[0].id,'task-1');
 assert.equal(store.getSnapshot().records[0].hidden,true);
 assert.equal(store.getSnapshot().records.at(-1).id,'task-13');
});
