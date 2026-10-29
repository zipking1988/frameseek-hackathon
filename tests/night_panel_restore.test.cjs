const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const model = require('../src/frameseek/static/workbench-model.js');

// Execute the actual restore function against desktop layout measurements.
function restore(saved, viewport) {
  const values = new Map(), widths = {source:384, inspector:344};
  const shell = {classList:{contains:()=>false},style:{
    setProperty(key,value) {values.set(key,value);},removeProperty(key) {values.delete(key);}
  }};
  const panel = side => ({getBoundingClientRect:()=>({width:parseFloat(values.get(`--${side}-user-width`)) || widths[side]})});
  const context = vm.createContext({panelWidths:{source:null,inspector:null},
    window:{innerWidth:viewport,FrameSeekWorkbenchModel:model},
    $:id=>panel(id==='source-dock'?'source':'inspector'),
    document:{querySelector:selector=>selector==='.agent-shell'?shell:{setAttribute(){}}}
  });
  const source=fs.readFileSync('src/frameseek/static/app.js','utf8');
  vm.runInContext(source.slice(source.indexOf('function restorePanelWidths('),source.indexOf('function setupPanelResize(')),context);
  context.restorePanelWidths(saved);
  return context.panelWidths;
}

test('restoring widths saved on a wide desktop preserves a 420px canvas on a narrower desktop',()=>{
  const wide=restore({sourceWidth:478,inspectorWidth:453},2337);
  assert.equal(wide.source,478);assert.equal(wide.inspector,453);
  const narrow=restore({sourceWidth:478,inspectorWidth:453},1280);
  assert.ok(narrow.source>=280);assert.ok(narrow.inspector>=260);
  assert.ok(1280-56-narrow.source-narrow.inspector>=420);
});
