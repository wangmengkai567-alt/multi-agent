/* Concurrent same-kind agent calls must remain distinguishable after replay. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
const element = selector => {
  if (!elements.has(selector)) elements.set(selector,{value:'',innerHTML:'',textContent:'',hidden:false,checked:false,
    scrollHeight:0,scrollTop:0,clientHeight:0,addEventListener(){},querySelectorAll(){return [];},querySelector(){return {innerHTML:'',textContent:''};}});
  return elements.get(selector);
};
class Stream { close(){} }
const context = {document:{querySelector:element},localStorage:{getItem(){return null;},setItem(){}},
  RunEventStream:Stream,WebSocket:{OPEN:1},URLSearchParams,console,
  fetch:async()=>({ok:true,json:async()=>({files:[]})})};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../api/static/app.js'),'utf8')+
  ';this.test={callGroups,renderCallGroups,createSession,applyEvent,displayStatus,renderJobs};',context);
const ui=context.test, session=ui.createSession('thread','城市比较','run');
let seq=0;
const event=(kind,call,parent,actor,status,data={})=>({thread_id:'thread',run_id:'run',event_seq:++seq,
  event:kind,call_id:call,parent_call_id:parent,actor,status,data,timestamp:'2026-10-19T09:00:00Z'});
const events=[
 event('assistant_call','request-a','run','search','requested'),
 event('assistant_call','request-b','run','search','requested'),
 event('subagent_running','agent-a','request-a','search','running',{request_call_id:'request-a',target:'search',task_label:'晋城'}),
 event('subagent_running','agent-b','request-b','search','running',{request_call_id:'request-b',target:'search',task_label:'新乡'}),
 event('before_tool','search-a','agent-a','search:internet_search','running'),
 event('before_tool','search-b','agent-b','search:internet_search','running'),
 event('after_tool','search-b','agent-b','search:internet_search','completed'),
 event('on_error','search-a','agent-a','search:internet_search','rejected',{error_category:'search_limit'}),
 event('subagent_failed','agent-a','request-a','search','failed',{request_call_id:'request-a',target:'search'}),
 event('subagent_completed','agent-b','request-b','search','completed',{request_call_id:'request-b',target:'search'}),
 event('tool_returned','agent-a','run','task','returned') // Legacy parent inconsistency must still replay.
];
events.push(event('after_tool','legacy-reused','agent-a','search:read_file','completed'));
events.push(event('after_tool','legacy-reused','agent-b','search:read_file','completed'));
for (const e of [...events].reverse()) ui.applyEvent(session,e);
const groups=ui.callGroups(session);
assert.equal(groups.length,2);
const a=groups.find(g=>g.id==='agent-a'),b=groups.find(g=>g.id==='agent-b');
assert.equal(a.label,'晋城');assert.equal(b.label,'新乡');
assert.equal(a.events.filter(e=>e.call_id==='legacy-reused').length,1);
assert.equal(b.events.filter(e=>e.call_id==='legacy-reused').length,1);
assert.ok(a.events.some(e=>e.call_id==='search-a'));assert.ok(!a.events.some(e=>e.call_id==='search-b'));
assert.ok(b.events.some(e=>e.call_id==='search-b'));
const html=ui.renderCallGroups(session);
assert.match(html,/晋城.*未通过/);assert.match(html,/新乡.*检查通过/);
assert.equal((html.match(/data-call="search-a"/g)||[]).length,1);
assert.equal((html.match(/data-call="search-b"/g)||[]).length,1);
const before=session.logs.length;ui.applyEvent(session,events[0]);assert.equal(session.logs.length,before);
// Same-kind success cannot erase another instance's failed state.
session.status='completed';assert.equal(ui.displayStatus(session),'partial');
ui.applyEvent(session,event('run_timed_out','run',null,'main','timed_out',{partial_result:'已经核实的部分',error:'总时长耗尽'}));
assert.equal(session.answer,'已经核实的部分');assert.equal(session.status,'timed_out');
session.jobs=[{task_id:'job-a',status:'working',poll_after_seconds:3}];ui.renderJobs(session);
assert.match(element('#jobArea').innerHTML,/后台执行中/);assert.doesNotMatch(element('#jobArea').innerHTML,/研究任务.*已完成/);
session.jobs=[{task_id:'job-a',status:'completed',result:{answer:'真实完成结果'}}];ui.renderJobs(session);
assert.match(element('#jobArea').innerHTML,/已完成/);
// A failed final review must stay partial even when all child agents passed.
session.status='completed';session.jobs=[];session.deliveryStatus='partial';
assert.equal(ui.displayStatus(session),'partial');
console.log('PASS: concurrent agent ownership, tool aggregation, reverse replay, failed-instance retention, partial timeout and background status');
