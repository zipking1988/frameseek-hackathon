const test=require('node:test');
const assert=require('node:assert/strict');
const report=require('../src/frameseek/static/investigation-report.js');
const frame={t:6.125,uri:'/media/demo_parcel/frames/6.jpg',frame_id:'f6',caption:'箱子位于中央'};
const query=(id,status='partial')=>({id,question:'在哪里？',mode:'fixture',response:{status,answer:'可见三个位置阶段。',unresolved:['连续移动未确认'],errors:[],events:status==='partial' ? [{video_id:'demo_parcel',start:5,end:11,summary:'位于中央',support_level:'medium',evidence:[{...frame}]}] : []}});
test('reports retain orphaned pin origins, context, exact timestamps and a frozen snapshot',()=>{
 const q=query('q1'),q2=query('q2','no_result');q2.parentTurn='turn-1';
 const state={title:'位置调查',note:'待核实',records:[{id:'turn-2',query:q2}],pins:[{id:'p1',kind:'evidence',turnId:'turn-1',title:'证据',query:q,frame}],canvas:{nodes:[{id:'turn-1',x:1,y:2}]},ui:{draft:'未发送私有草稿'}};
 const result=report.create(state,'2026-10-05');state.note='新笔记';q.response.answer='新答案';
 assert.equal(result.questions.length,2);assert.equal(result.questions[1].retained_by_finding,true);assert.equal(result.questions[0].parent_turn_id,'turn-1');
 assert.equal(result.evidence[0].t,6.125);assert.equal(result.note,'待核实');assert.ok(!JSON.stringify(result).includes('未发送私有草稿'));
 assert.match(report.markdown(result),/连续移动未确认/);assert.match(report.markdown(result),/no_result/);assert.match(report.markdown(result),/未调用模型/);
});
test('HTML escapes every text field and embeds only inert raster data URLs',()=>{
 const q=query('q1');q.question='<script>alert(1)</script>';q.response.answer='</p><img src=x onerror=evil()>';
 const result=report.create({title:'</title><script>evil()</script>',note:'<iframe src=https://evil.test>',records:[{id:'t1',query:q}],pins:[]});
 const html=report.html(result,{E1:'https://evil.test/image'});
 assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<iframe'));assert.ok(!html.includes('src="https://'));assert.match(html,/&lt;script&gt;/);assert.match(html,/default-src 'none'/);assert.match(html,/0\/1 张已嵌入/);
 assert.match(report.html(result,{E1:'data:image/png;base64,aGVsbG8='}),/1\/1 张已嵌入/);
 assert.ok(!report.html(result,{E1:'data:image/svg+xml;base64,aGVsbG8='}).includes('<img src='));
});
test('reports reject cross-source evidence, nonfinite time, external and traversal media references',()=>{
 const q=query('q1');q.response.events[0].video_id='other';assert.throws(()=>report.create({records:[{query:q}]}),/其他素材/);
 q.response.events[0].video_id='demo_parcel';q.response.events[0].evidence[0].uri='https://evil.test';assert.throws(()=>report.create({records:[{query:q}]}),/引用无效/);
 assert.equal(report.safeFrameUri('demo_parcel','/media/demo_parcel/frames/../x.jpg'),false);
 assert.equal(report.safeFrameUri('other','/media/demo_parcel/frames/x.jpg'),false);
 q.response.events[0].evidence[0].uri=frame.uri;q.response.events[0].evidence[0].t=NaN;assert.throws(()=>report.create({records:[{query:q}]}),/引用无效/);
});
test('real and unindexed sources have distinct provenance, errors survive exports, repeated frames deduplicate',()=>{
 const q=query('q1'),q2=query('q2');q2.response.status='error';q2.response.errors=[{code:'timeout',message:'后续处理超时'}];
 const saved=report.create({records:[{id:'t1',query:q},{id:'t2',query:q2}],pins:[]});assert.equal(saved.evidence.length,1);assert.deepEqual(saved.evidence[0].query_ids,['q1','q2']);assert.match(report.html(saved),/后续处理超时/);
 const real=report.create({sourceInfo:{id:'real_a',name:'实际录像',metadata:{duration:100}},records:[],pins:[]});assert.match(report.provenance(real),/真实素材/);
 assert.match(report.provenance(report.create({local:true,records:[],pins:[]})),/不作分析结论/);
});

test('repeated fixture request IDs retain every conversation turn and its pin origin',()=>{
 const q=query('center'),q2=query('center');q2.parentTurn='t1';q2.response.answer='第二轮不同答案';
 const state={records:[{id:'t1',query:q},{id:'t2',query:q2}],pins:[{id:'p1',kind:'conclusion',turnId:'t1',query:q,title:'第一轮'}]};
 const result=report.create(state);assert.equal(result.questions.length,2);assert.equal(result.questions[1].parent_turn_id,'t1');
 assert.deepEqual(result.evidence[0].turn_ids,['t1','t2']);assert.match(report.markdown(result),/第二轮不同答案/);assert.match(report.markdown(result),/来源问题：在哪里？/);
 assert.match(report.html(result),/第一轮/);assert.match(report.html(result),/第二轮不同答案/);
});


test('human-readable reports preserve subsecond evidence intervals and source speech',()=>{
 const q=query('speech');const evidence=q.response.events[0].evidence[0];evidence.caption=null;evidence.asr='立即停止';q.response.events[0].start=6.125;q.response.events[0].end=6.875;
 const result=report.create({records:[{id:'t1',query:q}],pins:[{id:'p1',kind:'evidence',turnId:'t1',query:q,frame:evidence,title:'原声'}]});
 for(const text of [report.markdown(result),report.html(result)]) {assert.match(text,/00:00:06.125/);assert.match(text,/00:00:06.875/);assert.match(text,/立即停止/);}
 assert.equal(report.time(59.9999),'00:01:00');
});
test('deduplicated frame images preserve differing observations from multiple turns',()=>{
 const q=query('first'),q2=query('second');q2.response.events[0].evidence[0].caption='第二轮可见标签';
 const result=report.create({records:[{id:'t1',query:q},{id:'t2',query:q2}],pins:[]});assert.equal(result.evidence.length,1);assert.equal(result.evidence[0].observations.length,2);
 for(const text of [report.markdown(result),report.html(result)]){assert.match(text,/箱子位于中央/);assert.match(text,/第二轮可见标签/);}
});
