const test=require('node:test');
const assert=require('node:assert/strict');
const {composerSuggestions}=require('../src/frameseek/static/workbench-model.js');
const record=(extra={})=>({id:'task-1',query:{id:'q1',mode:'real',video_id:'basketball',response:{status:'partial',answer:'Visible scene',events:[{video_id:'basketball',evidence:[{t:326.1,uri:'/media/basketball/frames/one.jpg',caption:'Scoreboard'}]}],followups:['A?','B?','A?','C?','D?']}},...extra});
const state=(rec=record())=>({sourceInfo:{id:'basketball',metadata:{duration:384}},selectedId:'task-1',records:[rec]});
test('cached model followups retain priority with the original source-frame provenance',()=>{
 const result=composerSuggestions(state());
 assert.equal(result.label,'Suggested follow-ups');assert.deepEqual(result.items,['A?','B?','C?']);
 assert.equal(result.basis.t,326.1);assert.equal(result.basis.queryId,'q1');
});
test('wrong-video, hidden, failed and evidence-free answers never supply grounded suggestions',()=>{
 const wrong=record();wrong.query.video_id='other';
 const failed=record();failed.query.response.status='error';
 const empty=record();empty.query.response.events=[];
 for(const r of [wrong,failed,empty,record({hidden:true})]){
  const result=composerSuggestions(state(r));assert.match(result.label,/Example questions/);assert.equal(result.basis,undefined);
 }
});
test('box suggestions stay synthetic and cannot reuse basketball evidence',()=>{
 const s=state();s.sourceInfo={id:'demo_parcel'};assert.equal(composerSuggestions(s).label,'Demo questions');
 const r=record();r.query.mode='fixture';r.query.response.events=[{video_id:'demo_parcel',evidence:[{t:6,uri:'/media/demo_parcel/frames/6.jpg'}]}];
 assert.equal(composerSuggestions({sourceInfo:{id:'demo_parcel'},selectedId:'task-1',records:[r]}).label,'Demo follow-ups');
});
test('basketball questions use saved scoreboard/clock observations and bounded real source timestamps',()=>{
 const r=record();r.query.response.followups=[];r.query.response.answer='记分牌上显示第四节比赛时间还剩16.3秒。';
 const result=composerSuggestions(state(r));
 assert.deepEqual(result.titles,['Check the scoreboard','Read the game clock','Review what changed']);
 assert.equal(result.label,'Based on saved evidence');assert.equal(result.basis.t,326.1);
 assert.ok(result.items.every(q=>q.startsWith('Review the video from 05:20 to 05:33: ')));
 assert.ok(result.items.every(q=>!q.includes('16.3')));
});
test('real box evidence generates object questions even if its filename misleadingly says basketball',()=>{
 const r=record();r.query.response.followups=[];r.query.video_id='box';r.query.response.answer='纸箱位于工作台中央。';
 r.query.response.events=[{video_id:'box',evidence:[{t:1,uri:'/media/box/frames/one.jpg',caption:'纸箱位于工作台中央'}]}];
 const result=composerSuggestions({sourceInfo:{id:'box',name:'basketball.mp4',metadata:{duration:5}},records:[r]});
 assert.deepEqual(result.titles,['Did the box move?','What is near the box?','Review what changed']);
 assert.ok(result.items.every(q=>q.startsWith('Review the video from 00:00 to 00:05: ')));
});
test('deselecting still uses the latest valid visible evidence; hiding it removes its recommendations',()=>{
 const r=record();r.query.response.followups=[];
 const s={...state(r),selectedId:null};assert.equal(composerSuggestions(s).basis.queryId,'q1');
 r.hidden=true;assert.equal(composerSuggestions(s).basis,undefined);
});
test('nonfinite/out-of-duration frames, foreign event IDs and external media never anchor recommendations',()=>{
 for(const mutation of [r=>r.query.response.events[0].evidence[0].t=Infinity,
  r=>r.query.response.events[0].evidence[0].t=9999,r=>r.query.response.events[0].video_id='foreign',
  r=>r.query.response.events[0].evidence[0].uri='https://example.com/one.jpg']){
   const r=record();mutation(r);assert.equal(composerSuggestions(state(r)).basis,undefined);
 }
});
test('cached followups from a previous good answer include a time window when no context is selected',()=>{
 const result=composerSuggestions({...state(),selectedId:null});
 assert.ok(result.items[0].startsWith('Review the video from 05:20 to 05:33: '));
});


test('known countdown contradiction is flagged without rewriting historical model output',()=>{
 const {answerReviewNotes}=require('../src/frameseek/static/workbench-model.js');
 const answer='比赛计时从2:16递减至2:07。右侧数字从14递减至23。';
 const notes=answerReviewNotes(answer);assert.equal(notes.length,1);assert.match(notes[0],/14 → 23/);
 assert.equal(answer,'比赛计时从2:16递减至2:07。右侧数字从14递减至23。');
 assert.deepEqual(answerReviewNotes('数字从23递减至14，从14递增至23。'),[]);
 assert.equal(answerReviewNotes('数字从23上升至14。').length,1);
});


test('portable reports retain the numeric-review caution without changing the model response',()=>{
 const report=require('../src/frameseek/static/investigation-report.js');
 const r=record();r.query.response.answer='右侧数字从14递减至23。';
 r.query.response.unresolved=[];r.query.response.errors=[];
 const original=JSON.stringify(r.query.response);
 const output=report.create({...state(r),pins:[]});
 assert.equal(output.questions[0].review_notes.length,1);
 assert.match(report.markdown(output),/Text review/);
 assert.match(report.html(output),/Text review/);
 assert.equal(JSON.stringify(r.query.response),original);
 assert.equal(output.questions[0].response.answer,'右侧数字从14递减至23。');
});
