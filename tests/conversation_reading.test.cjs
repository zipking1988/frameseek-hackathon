const test = require('node:test');
const assert = require('node:assert/strict');
const {conversationAnswer} = require('../src/frameseek/static/workbench-model.js');
test('short text OCR is available alongside both partial hits and empty results',()=>{
  const {shouldOfferTextCandidates}=require('../src/frameseek/static/workbench-model.js');
  const q={engine:'index',question:'Find text "EXIT" from 00:00 to 00:02',response:{status:'partial'}};
  assert.equal(shouldOfferTextCandidates(q,true,()=>true),true);
  assert.equal(shouldOfferTextCandidates({...q,response:{status:'no_result'}},true,()=>true),true);
  assert.equal(shouldOfferTextCandidates({...q,response:{status:'error'}},true,()=>true),false);
  assert.equal(shouldOfferTextCandidates(q,false,()=>true),false);
  assert.equal(shouldOfferTextCandidates(q,true,()=>false),false);
  assert.equal(shouldOfferTextCandidates({...q,index_search:{ocr_eligible:false}},true,()=>true),false);
});
const query = () => ({mode:'real',engine:'local',video_id:'video-A',question:'多少个三分球投进？',response:{status:'partial',answer:'1个',unresolved:[],events:[{video_id:'video-A',evidence:[
  {frame_id:'second',t:102,uri:'/media/video-A/frames/second.jpg'},
  {frame_id:'first',t:101,uri:'/media/video-A/frames/first.jpg'},
]}]}});
test('reading presentation distinguishes estimate, event count and evidence without mutating the answer',()=>{
  const q=query(),before=JSON.stringify(q),view=conversationAnswer(q);
  assert.equal(view.text,'1个');assert.match(view.prefix,/估计/);assert.match(view.caution,/尚未核实/);
  assert.match(view.evidenceNote,/不代表事件发生次数/);assert.deepEqual(view.frames.map(f=>f.t),[101,102]);
  assert.equal(JSON.stringify(q),before);
});
test('reading view preserves all references while excluding foreign or invalid images',()=>{
  const q=query(),e=q.response.events[0].evidence;
  e.push(e[0],{frame_id:'bad',t:0,uri:'https://example.com/a.jpg'},{frame_id:'nan',t:NaN,uri:'/media/video-A/frames/x.jpg'});
  q.response.events.push({video_id:'video-B',evidence:[{t:1,uri:'/media/video-B/frames/a.jpg'}]});
  assert.equal(conversationAnswer(q).frames.length,2);
  q.response.events[0].evidence=Array.from({length:377},(_,i)=>({frame_id:`f${i}`,t:i,uri:`/media/video-A/frames/f${i}.jpg`}));
  assert.equal(conversationAnswer(q).frames.length,377);
});
test('synthetic box answers retain explicit fixture provenance and their real demo frame links',()=>{
  const q={question:'箱子在哪里？',response:{status:'ok',answer:'中央。',unresolved:[],events:[{video_id:'demo_parcel',evidence:[{frame_id:'f',t:6,uri:'/media/demo_parcel/frames/6.jpg'}]}]}};
  const view=conversationAnswer(q);assert.match(view.label,/预设回答/);assert.equal(view.prefix,'');assert.equal(view.frames.length,1);
});
test('English preset translation is limited to the exact synthetic example',()=>{
  const q={id:'center',question:'When is the parcel in the center?',response:{status:'ok',answer:'包裹在 00:05–00:11 出现在工作台中央。',unresolved:[],events:[{video_id:'demo_parcel',evidence:[{frame_id:'f',t:6,uri:'/media/demo_parcel/frames/6.jpg'}]}]}};
  const before=JSON.stringify(q),view=conversationAnswer(q);
  assert.equal(view.text,'The parcel appears in the center from 00:05 to 00:11.');
  assert.match(view.label,/Synthetic demo · preset answer/);assert.equal(view.englishDemo,true);
  assert.equal(view.frames[0].t,6);assert.equal(JSON.stringify(q),before);
  assert.equal(conversationAnswer({...q,mode:'real',video_id:'demo_parcel'}).text,q.response.answer);
  assert.equal(conversationAnswer({...q,question:'Other question'}).text,q.response.answer);
  assert.equal(conversationAnswer({...q,synthetic_fixture:true}).text,q.response.answer);
  const {replyQuote}=require('../src/frameseek/static/workbench-model.js');
  assert.deepEqual(replyQuote([{id:'task-1',query:q}],q),{
    heading:'Replying to answer 1',question:q.question,
    answer:'The parcel appears in the center from 00:05 to 00:11.',englishDemo:true,
  });
  assert.equal(JSON.stringify(q),before);
});
test('no-result and error states preserve their original explanation rather than inventing an estimate',()=>{
  for(const status of ['error','no_result']){
    const q=query();q.response.status=status;q.response.answer='未完成这次分析';q.response.events=[];
    const view=conversationAnswer(q);assert.equal(view.text,q.response.answer);assert.equal(view.caution,'');assert.deepEqual(view.frames,[]);
  }
});
test('English fixture follow-up retains the real demo frame and cannot translate private or real answers',()=>{
  const q={id:'right',question:'What happens next?',response:{status:'ok',answer:'从 00:11 开始，包裹出现在工作台右侧。',unresolved:[],events:[{video_id:'demo_parcel',evidence:[{frame_id:'f-right',t:13,uri:'/media/demo_parcel/frames/13.jpg'}]}]}};
  const before=JSON.stringify(q),view=conversationAnswer(q);
  assert.equal(view.text,'From 00:11, the parcel appears on the right side of the workbench.');
  assert.equal(view.englishDemo,true);assert.equal(view.frames[0].t,13);
  assert.equal(JSON.stringify(q),before);
  for(const changes of [{mode:'real'},{synthetic_fixture:true},{video_id:'private-video'},{question:'Find the defender'}]){
    assert.equal(conversationAnswer({...q,...changes}).text,q.response.answer);
    assert.equal(conversationAnswer({...q,...changes}).englishDemo,false);
  }
  const failed={...q,response:{...q.response,status:'error'}};
  assert.equal(conversationAnswer(failed).englishDemo,false);
});

test('unconfirmed supplements keep checked frames separate from matching evidence',()=>{
 const q=query();q.mode='real';q.engine='index';q.video_id='vid_a';
 q.response.status='no_result';q.response.events=[];
 q.index_search={kind:'supplement_unconfirmed',checked_frames:[
  null,
  {frame_id:'vid_a_f1',t:1,uri:'/media/vid_a/frames/one.jpg'},
  {frame_id:'vid_b_f2',t:2,uri:'/media/vid_b/frames/two.jpg'},
  {frame_id:'vid_a_f3',t:3,uri:'/media/vid_a/frames/../bad.jpg'},
  {frame_id:'vid_a_f4',t:4,uri:'/media/vid_a/frames/%2e%2e.jpg'},
 ]};
 const view=conversationAnswer(q);
 assert.deepEqual(view.frames,[]);
 assert.deepEqual(view.checkedFrames,[q.index_search.checked_frames[1]]);
 assert.deepEqual(conversationAnswer({...q,index_search:{...q.index_search,kind:'indexed_occurrences'}}).checkedFrames,[]);
});

test('actual canvas handlers leave conversation wheel and summary clicks to the browser',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const source=fs.readFileSync(require.resolve('../src/frameseek/static/investigation-canvas.js'),'utf8');
  let prevented=0,panned=0,captured=0;
  const viewport={dataset:{readingMode:'true'},setPointerCapture(){captured++;},classList:{add(){}},getBoundingClientRect(){return {left:0,top:0};}};
  const sandbox={viewport,gesture:null,model:{pan(){panned++;},zoom(){},view:{scale:1}},scheduleDraw(){},detailFocus:null};
  const prefix='    viewport.addEventListener("pointerdown", ';
  const begin=source.indexOf(prefix),end=source.indexOf('    });',begin);
  const pointer=vm.runInNewContext('('+source.slice(begin+prefix.length,end)+'})',sandbox);
  const m=source.match(/"wheel",\s*(\(e\) => \{[\s\S]*?)\n      \},\n      \{ passive: false \}/);
  assert.ok(m);
  const wheel=vm.runInNewContext('('+m[1]+'})',sandbox);
  const event={button:0,target:{closest(){return null;}},preventDefault(){prevented++;},deltaX:0,deltaY:30,clientX:0,clientY:0};
  pointer(event);wheel(event);
  assert.equal(captured,0);assert.equal(prevented,0);assert.equal(panned,0);
  viewport.dataset.readingMode='false';pointer(event);wheel(event);
  assert.equal(captured,1);assert.equal(prevented,1);assert.equal(panned,1);
});

test('reading scoreboard time questions are not mislabeled as whole-video event counts',()=>{
  const q=query();q.question='第四节比赛时间还剩多少秒？';q.response.answer='16.3秒';
  const view=conversationAnswer(q);assert.equal(view.prefix,'');assert.equal(view.caution,'');assert.match(view.evidenceNote,/回看原视频/);
});

test('canvas composer entry opens conversation without submitting or changing a draft',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const source=fs.readFileSync(require.resolve('../frontend/workbench.jsx'),'utf8');
  const body=source.match(/const enterConversation = event => \{([\s\S]*?)\n    \};/)[1];
  const modes=[];const handle=vm.runInNewContext('(event)=>{'+body+'}',{setMode:mode=>modes.push(mode)});
  handle({type:'focusin',target:{id:'question'}});
  handle({type:'pointerdown',target:{closest:()=>null}});
  handle({type:'focusin',target:{id:'chat-engine'}});
  handle({type:'pointerdown',target:{closest:()=>({})}});
  assert.deepEqual(modes,['conversation','conversation']);
});

test('replaying another answer does not change the draft reply context',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const source=fs.readFileSync(require.resolve('../frontend/workbench.jsx'),'utf8');
  const body=source.match(/const replay = frame => \{([^\n]+)\};/)[1];
  const actions=[],query={id:'old-answer'},record={id:'task-old'};
  const replay=vm.runInNewContext('(frame)=>{'+body+'}',{
    ui:()=>({select:id=>actions.push(['select',id]),seek:(time,item)=>actions.push(['seek',time,item.id])}),
    record,query,
  });
  replay({t:88});
  assert.deepEqual(actions,[['seek',88,'old-answer']]);
});

test('reply quotes distinguish duplicate questions using the actual context ID, not selection or text',()=>{
  const {replyQuote}=require('../src/frameseek/static/workbench-model.js');
  const first=query();first.id='q-first';
  const second=query();second.id='q-second';second.response.answer='无法确定';
  const records=[{id:'task-24',query:first},{id:'task-25',query:second}];
  const before=JSON.stringify(records);
  assert.deepEqual(replyQuote(records,second),{heading:'引用问题 25 的回答',question:second.question,answer:'无法确定'});
  assert.equal(replyQuote(records,null),null);
  assert.equal(replyQuote(records,{...second,video_id:'foreign'}),null);
  assert.equal(replyQuote([{...records[1],hidden:true}],second),null);
  assert.equal(JSON.stringify(records),before);
});

test('reply quote uses the selected turn when fixture questions reuse an ID',()=>{
  const {replyQuote}=require('../src/frameseek/static/workbench-model.js');
  const first=query();first.id='center';first.question='same';
  const second={...first,response:{...first.response,answer:'second answer'}};
  const records=[{id:'task-1',query:first},{id:'task-2',query:second}];
  assert.equal(replyQuote(records,second,'task-2').heading,'引用问题 2 的回答');
  assert.equal(replyQuote(records,second,'task-2').answer,'second answer');
  assert.equal(replyQuote(records,second,'task-unknown'),null);
});
