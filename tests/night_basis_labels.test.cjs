const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {transformSync} = require('esbuild');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const model = require('../src/frameseek/static/workbench-model.js');

function render(mode, events, errors = []) {
  const state = {epoch:0, selectedId:'task-1', inspectorTab:'context', pins:[],
    records:[{id:'task-1',query:{mode,question:'Question',response:{status:events.length?'partial':'error',
      answer:'Answer',events,errors,unresolved:[],trace:{visible:{segments_reviewed:events.length}}}}}]};
  const source = fs.readFileSync('frontend/workbench.jsx','utf8')
    .replace('const useDesk = () => useSyncExternalStore(store.subscribe, store.getSnapshot);', 'const useDesk = () => window.testState;')
    .split('createRoot(document.getElementById("workspace-chrome"))')[0] + '\nmodule.exports = Inspector;';
  const context = vm.createContext({require,module:{exports:{}},window:{FrameSeekWorkbenchModel:model,testState:state}});
  vm.runInContext(transformSync(source,{loader:'jsx',format:'cjs'}).code,context);
  return renderToStaticMarkup(React.createElement(context.module.exports));
}

test('an audio guard with zero evidence does not claim model review in the inspector',()=>{
  const html=render('real',[],[{code:'audio_channel_unavailable'}]);
  assert.match(html,/No valid evidence generated/);
  assert.doesNotMatch(html,/模型回看|检索与模型回看/);
});

test('failed visual calls with zero evidence do not imply successful review',()=>{
  const html=render('real',[],[{code:'analysis_failed'}]);
  assert.match(html,/No valid evidence generated/);
  assert.match(html,/not enough information/);
  assert.doesNotMatch(html,/请写出完整问题/);
});

test('historical failure codes explain the system failure without blaming the question or inventing a provider cause',()=>{
  assert.match(render('real',[],[{code:'invalid_tool_request'}]),/analysis tool request failed validation/);
  assert.match(render('real',[],[{code:'invalid_visual_matches'}]),/invalid match conditions, subjects, or frame references/);
  assert.match(render('real',[],[{code:'visual_index_invalid'}]),/frame index is missing, changed, or contains invalid references/);
  assert.match(render('real',[],[{code:'query_cancelled'}]),/search was cancelled/);
  assert.match(render('real',[],[{code:'audio_channel_unavailable'}]),/reliable audio transcript is unavailable/);
  assert.match(render('real',[],[{code:'real_blocked'}]),/blocked before a model call/);
  assert.match(render('real',[],[{code:'budget_blocked'}]),/Budget admission failed/);
  assert.doesNotMatch(render('real',[],[{code:'budget_blocked'}]),/not enough information/);
  assert.doesNotMatch(render('real',[],[{code:'analysis_failed'}]),/503|额度不足|问题写得不完整/);
});

test('real evidence and synthetic demos retain distinct basis descriptions',()=>{
  const events=[{evidence:[{uri:'/media/video/frames/one.jpg',t:60,caption:'Visible frame'}]}];
  assert.match(render('real',events),/Video evidence/);
  assert.match(render('fixture',events),/Preset match/);
});
