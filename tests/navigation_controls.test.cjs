const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {transformSync} = require('esbuild');
const model = require('../src/frameseek/static/workbench-model.js');

function harness() {
  const handlers = new Map();
  const menuHandlers = new Map();
  let focused = false;
  const trigger = {focus: () => {focused = true;}};
  const count = {};
  const menu = {open:true, querySelector:()=>trigger, contains:target=>target===trigger,
    addEventListener:(name,fn)=>menuHandlers.set(name,fn),
    removeEventListener:name=>menuHandlers.delete(name)};
  const document = {getElementById:id=>id==='header-more'?menu:count,
    addEventListener:(name,fn)=>handlers.set(name,fn),
    removeEventListener:name=>handlers.delete(name)};
  const cleanups=[];
  const react = {...require('react'), useEffect:fn=>{const cleanup=fn();if(cleanup)cleanups.push(cleanup);}};
  const source=fs.readFileSync('frontend/workbench.jsx','utf8')
    .replace('const useDesk = () => useSyncExternalStore(store.subscribe, store.getSnapshot);', 'const useDesk = () => ({pins:[]});')
    .split('createRoot(document.getElementById("workspace-chrome"))')[0]+'\nmodule.exports=WorkspaceChrome;';
  const context=vm.createContext({document,window:{FrameSeekWorkbenchModel:model},module:{exports:{}},
    require:name=>name==='react'?react:require(name)});
  vm.runInContext(transformSync(source,{loader:'jsx',format:'cjs'}).code,context);
  context.module.exports();
  return {menu,trigger,handlers,menuHandlers,cleanups,count,focused:()=>focused};
}

test('More dismisses on Escape and restores focus, and removes all listeners on unmount',()=>{
  const h=harness();let prevented=false;
  h.handlers.get('keydown')({key:'Escape',preventDefault:()=>{prevented=true;}});
  assert.equal(h.menu.open,false);assert.equal(h.focused(),true);assert.equal(prevented,true);
  h.cleanups.forEach(cleanup=>cleanup());
  assert.equal(h.handlers.size,0);assert.equal(h.menuHandlers.size,0);
});

test('More closes outside and after an action without stealing dialog focus',()=>{
  const h=harness();
  h.handlers.get('pointerdown')({target:h.trigger});assert.equal(h.menu.open,true);
  h.handlers.get('pointerdown')({target:{}});assert.equal(h.menu.open,false);
  h.menu.open=true;
  h.menuHandlers.get('click')({target:{closest:()=>({})}});
  assert.equal(h.menu.open,false);assert.equal(h.focused(),false);
  assert.equal(h.count.hidden,true);
});
