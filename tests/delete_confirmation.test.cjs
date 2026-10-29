const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const model=require('../src/frameseek/static/workbench-model.js');
function harness(){
  let state={epoch:1,records:[{id:'task-2',query:{id:'q2',question:'Failed basketball question'}}],pins:[],ui:{draft:'Keep this draft'}};
  const elements=new Map();let focused=null;
  const get=id=>{
    if(!elements.has(id))elements.set(id,{open:false,textContent:'',listeners:{},
      showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.({});},
      focus(){focused=id;},addEventListener(name,fn){this.listeners[name]=fn;}});
    return elements.get(id);
  };
  const context=vm.createContext({$:get,document:{activeElement:null},activeVideo:null,notice(){},window:{
    FrameSeekWorkbench:{getSnapshot:()=>state},FrameSeekWorkbenchModel:model,
    FrameSeekSessionUI:{snapshot:()=>state,restore:next=>{state=next;}}}});
  const app=fs.readFileSync('src/frameseek/static/app.js','utf8');
  vm.runInContext(app.slice(app.indexOf('let pendingTurnDeletion = null;'),app.indexOf('function openInspector(')),context);
  return {get,run:code=>vm.runInContext(code,context),state:()=>state,focused:()=>focused};
}

test('requesting deletion requires confirmation and initially focuses cancel',()=>{
  const h=harness();h.run('requestTurnDeletion("task-2")');
  assert.equal(h.get('delete-turn-dialog').open,true);
  assert.equal(h.get('delete-turn-question').textContent,'Failed basketball question');
  assert.equal(h.focused(),'delete-turn-cancel');
  assert.equal(h.state().records[0].hidden,undefined);
  h.get('delete-turn-cancel').listeners.click();
  assert.equal(h.get('delete-turn-dialog').open,false);
  assert.equal(h.state().records[0].hidden,undefined);
});

test('Escape cancels; confirmation hides recoverably while retaining draft and evidence',()=>{
  const h=harness();h.run('requestTurnDeletion("task-2")');let prevented=false;
  h.get('delete-turn-dialog').listeners.cancel({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(h.state().records[0].hidden,undefined);
  h.run('requestTurnDeletion("task-2")');h.get('delete-turn-confirm').listeners.click();
  assert.equal(h.state().records[0].hidden,true);
  assert.equal(h.state().ui.draft,'Keep this draft');
  assert.equal(h.get('delete-turn-dialog').open,false);
  h.run('changeTurnVisibility("task-2",false)');
  assert.equal(h.state().records[0].hidden,false);
});

test('a stale confirmation cannot delete a different question occupying the same card ID',()=>{
  const h=harness();h.run('requestTurnDeletion("task-2")');
  h.state().records[0].query={id:'different-query',question:'Do not delete'};
  h.get('delete-turn-confirm').listeners.click();
  assert.equal(h.state().records[0].hidden,undefined);
});
