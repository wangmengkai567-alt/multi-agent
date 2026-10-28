/* Concurrent same-kind agent calls must remain distinguishable after replay. */
const assert = require('node:assert/strict');
const {harness} = require('./browser-fixtures.cjs');
async function main() {
const {context,element,ui}=await harness();
const session=ui.createSession('thread','城市比较','run');
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
assert.match(html,/晋城.*执行失败/);assert.match(html,/新乡.*已完成/);
assert.equal((html.match(/data-call="search-a"/g)||[]).length,1);
assert.equal((html.match(/data-call="search-b"/g)||[]).length,1);
const before=session.logs.length;ui.applyEvent(session,events[0]);assert.equal(session.logs.length,before);
// Same-kind success cannot erase another instance's failed state.
session.status='completed';session.completion={status:'partial'};assert.equal(ui.displayStatus(session),'partial');
ui.applyEvent(session,event('run_timed_out','run',null,'main','timed_out',{error:'总时长耗尽'}));
assert.equal(session.answer,null);
assert.ok(session.logs.some(e=>e.event==='subagent_completed'&&e.call_id==='agent-b'));assert.equal(session.status,'timed_out');
// Server-owned partial coverage stays partial even when all child agents finished.
session.status='completed';session.completion={status:'partial'};
assert.equal(ui.displayStatus(session),'partial');
// A registered third expert carries its own display name and task ownership.
const custom=ui.createSession('thread','第三专家','custom-run');
for (const item of [
  event('subagent_running','audit-a','custom-run','auditor','running',
    {target:'auditor',assistant_name:'审计专家',task_label:'复核额度'}),
  event('after_tool','audit-tool','audit-a','auditor:execute_sql_query','completed'),
  event('subagent_completed','audit-a','custom-run','auditor','completed',
    {target:'auditor',assistant_name:'审计专家'})
]) ui.applyEvent(custom,{...item,run_id:'custom-run'});
const auditGroups=ui.callGroups(custom);
assert.equal(auditGroups.length,1);
assert.equal(auditGroups[0].target,'auditor');
assert.ok(auditGroups[0].events.some(e=>e.call_id==='audit-tool'));
assert.match(ui.renderCallGroups(custom),/审计专家 · 复核额度 · audit-a · 已完成/);
assert.doesNotMatch(ui.renderCallGroups(custom),/联网搜索助手|数据库查询助手/);
console.log('PASS: concurrent agent ownership, tool aggregation, reverse replay, failed-instance retention, retained child results on timeout and completion status');

}
main().catch(error=>{console.error(error);process.exitCode=1;});
