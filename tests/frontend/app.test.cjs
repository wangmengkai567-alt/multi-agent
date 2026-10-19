/* Execute the production UI in a VM; verify HTTP contracts and state transitions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../api/static/app.js'), 'utf8');
function harness(saved) {
  const elements = new Map(), calls = [], streams = [];
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {value:'', checked:false, innerHTML:'', textContent:'',
      hidden:false, scrollHeight:0, scrollTop:0, clientHeight:0, addEventListener(){}, querySelectorAll(){return [];}});
    return elements.get(selector);
  };
  element('#modeSelect').value = 'ordinary';
  let stored = saved ? JSON.stringify(saved) : null;
  class Stream {
    constructor(threadId,runId,onEvent,onState) { Object.assign(this,{threadId,runId,onEvent,onState,seen:new Set(),afterSeq:0}); streams.push(this); }
    connect() { this.connected = true; }
    close() { this.closed = true; }
  }
  const context = {document:{querySelector:element}, localStorage:{getItem(){return stored;},setItem(k,v){stored=v;}},
    RunEventStream:Stream, WebSocket:{OPEN:1}, console, URLSearchParams,
    fetch:async (url,options) => { calls.push({url,options}); return context.reply(url,options); },
    reply:() => ({ok:true,status:200,json:async()=>({files:[]})})};
  vm.createContext(context);
  vm.runInContext(source + ';this.ui={sessions,applyEvent,displayStatus,describeEvent,createSession,syncSession,sendTask,cancelRun,restoreSessions,closeSession,escapeHtml,refreshFiles};',context);
  return {context,ui:context.ui,element,calls,streams,stored:()=>JSON.parse(stored)};
}
const response = (data,status=200) => ({ok:status<400,status,json:async()=>data});
const ev = (n,event,status,data={}) => ({thread_id:'thread',run_id:'run',event_seq:n,event,status,data,actor:'search',call_id:'call',timestamp:'2026-10-17T00:00:00Z'});
const history = (status='running',answer=null,events=[]) => ({run:{run_id:'run',thread_id:'thread',status,answer,error:null},events});
async function main() {
  const h=harness(), {ui,element,context}=h;
  const s=ui.createSession('thread','中文问题','run');
  context.reply=url=>response(url.includes('/history') ? history('running',null,[ev(1,'run_started','running')]) : {files:[]});
  await ui.syncSession(s);
  assert.equal(s.status,'running'); assert.equal(h.streams[0].afterSeq,1);
  assert.deepEqual(h.stored().runs,[{threadId:'thread',runId:'run',query:'中文问题'}]);
  ui.applyEvent(s,ev(2,'skill_state','loaded',{skill_id:'ecommerce-analysis'}));
  ui.applyEvent(s,ev(3,'subagent_review','insufficient',{issues:['缺少来源']}));
  ui.applyEvent(s,ev(4,'subagent_refinement','insufficient',{revision_instruction:'补查来源'}));
  ui.applyEvent(s,ev(5,'subagent_review','passed'));
  ui.applyEvent(s,ev(6,'task_result','completed',{result:'完整答案'.repeat(500)}));
  ui.applyEvent(s,ev(6,'task_result','completed',{result:'重复'}));
  assert.equal(s.logs.length,6); assert.equal(ui.displayStatus(s),'completed');
  assert.equal(element('#answer').textContent,'完整答案'.repeat(500));
  assert.match(element('#logArea').innerHTML,/正文已读取/); assert.match(element('#logArea').innerHTML,/补查来源/);
  assert.ok(h.streams[0].closed);
  s.degraded=true; assert.equal(ui.displayStatus(s),'partial');
  for (const state of ['cancelled','timed_out','interrupted','failed']) {
    ui.applyEvent(s,ev(s.logs.length+1,state==='failed'?'error':'run_'+state,state,{error:'原因'}));
    assert.equal(ui.displayStatus(s),state); assert.match(element('#runSummary').innerHTML,/原因/);
  }
  assert.equal(ui.escapeHtml('<script>"\''),'&lt;script&gt;&quot;&#39;');
  assert.match(ui.describeEvent(ev(25,'on_error','rejected',{error_category:'search_limit'})),/已拦截新的请求/);
  assert.match(ui.describeEvent(ev(26,'after_tool','rejected',{error_category:'search_limit'})),/未执行/);
  assert.match(ui.describeEvent(ev(27,'review_repair','running')),/仅重试检查/);
  assert.equal(ui.describeEvent(ev(28,'on_error','cancelled',{error:'<object object at 0x0000014A534411F0>'})), '操作已取消：当前操作已取消');
  assert.match(ui.describeEvent(ev(29,'search_finalizing','partial')),/汇总已核实内容/);
  const quotaHarness = harness();
  const quota = quotaHarness.ui.createSession('thread','额度控制','quota');
  quota.status = 'completed';
  quota.logs = [{...ev(1,'after_tool','completed'),actor:'search:internet_search'},
    {...ev(2,'after_tool','rejected',{error_category:'search_limit'}),actor:'search:internet_search'}];
  assert.equal(quotaHarness.ui.displayStatus(quota),'completed');
  const finalCheck = quotaHarness.ui.createSession('thread','最终检查','final-check');
  finalCheck.status='completed';
  finalCheck.logs=[{...ev(1,'answer_validation','insufficient'),call_id:'final-check'}];
  assert.equal(quotaHarness.ui.displayStatus(finalCheck),'partial');
  finalCheck.logs.push({...ev(2,'answer_validation','passed'),call_id:'final-check'});
  assert.equal(quotaHarness.ui.displayStatus(finalCheck),'completed');

  finalCheck.logs.push({...ev(3,'subagent_review','insufficient'),call_id:'earlier-attempt'});
  assert.equal(quotaHarness.ui.displayStatus(finalCheck),'partial');
  finalCheck.logs.push({...ev(4,'answer_validation','passed',{answer_kind:'complete'}),call_id:'final-check'});
  assert.equal(quotaHarness.ui.displayStatus(finalCheck),'completed');
  finalCheck.logs.push({...ev(5,'answer_validation','partial',{answer_kind:'partial'}),call_id:'final-check'});
  assert.equal(quotaHarness.ui.displayStatus(finalCheck),'partial');

  assert.match(ui.describeEvent(ev(30,'subagent_review','dependency_unavailable',{issues:['缺配置']})),/依赖不可用.*缺配置/);
  assert.match(ui.describeEvent(ev(31,'subagent_refinement','insufficient',{
    revision_instruction:'[REDACTED]',issues:['缺少检索日期']})),/补充查询：缺少检索日期/);
  // Refresh restores server-owned full answers, never a truncated event preview.
  context.reply=url=>response(url.includes('/history') ? history('completed','恢复后的完整答案',[ev(1,'run_started','running')]) : {files:[]});
  await ui.restoreSessions(); assert.equal(s.answer,'恢复后的完整答案');
  // A terminal event that arrives during HTTP must not revert to a running snapshot.
  context.reply=url=>{ if(url.includes('/history')) ui.applyEvent(s,ev(31,'run_timed_out','timed_out')); return response(url.includes('/history')?history():{files:[]}); };
  await ui.syncSession(s); assert.equal(s.status,'timed_out');
  // Explicit business mode and Skill selection go through the actual submission code.
  element('#queryInput').value='待提交'; element('#modeSelect').value='internal_external'; element('#threadSelect').value='thread';
  context.reply=(url,options)=>response(url==='/api/task'?{status:'busy',thread_id:'thread',run_id:'run'}:url.includes('/history')?history():{files:[]},url==='/api/task'?409:200);
  await ui.sendTask();
  const body=JSON.parse(h.calls.find(c=>c.url==='/api/task').options.body);
  assert.equal(body.business_mode,'internal_external'); assert.deepEqual(body.skill_ids,['ecommerce-analysis']);
  assert.equal(body.thread_id,'thread'); assert.match(element('#notice').textContent,/忙碌/); assert.equal(element('#queryInput').value,'待提交');
  // Validation errors preserve user input and restore the submit button.
  context.reply=()=>response({detail:[{msg:'validation rejected'}]},422);
  await ui.sendTask(); assert.match(element('#notice').textContent,/validation rejected/); assert.equal(element('#sendBtn').disabled,false);
  // Cancel uses run identity and accepts a completion/cancellation race from the server.
  s.status='running';
  context.reply=url=>response(url.endsWith('/cancel')?{status:'completed',answer:'先完成了',error:null}:url.includes('/history')?history('completed','先完成了'):{files:[]});
  await ui.cancelRun(); assert.equal(s.status,'completed'); assert.equal(s.answer,'先完成了');
  assert.ok(h.calls.some(c=>c.url==='/api/runs/run/cancel'&&c.options.method==='POST'));
  // Files are scoped by run and encoded once, including non-ASCII and spaces.
  context.reply=()=>response({files:[{name:'报告.md',size:12,path:'D:\\output\\session_thread\\run_run\\中文 报告.md'}]});
  await ui.refreshFiles(); assert.match(h.calls.at(-1).url,/path=session_thread%2Frun_run/);
  assert.match(element('#fileList').innerHTML,/path=D%3A%5Coutput/); assert.doesNotMatch(element('#fileList').innerHTML,/%253A/);
  ui.closeSession('run'); assert.equal(Object.keys(ui.sessions).length,0);
  assert.equal(h.stored().runs.length,0);
  const reordered=ui.createSession('thread','乱序','run');
  ui.applyEvent(reordered,ev(3,'task_result','completed',{result:'正确答案'}));
  ui.applyEvent(reordered,ev(1,'run_queued','queued'));
  ui.applyEvent(reordered,ev(2,'run_started','running'));
  assert.equal(reordered.status,'completed'); assert.equal(reordered.answer,'正确答案');
  ui.applyEvent(reordered,{...ev(4,'after_tool','failed'),actor:'main:write_file'});
  assert.equal(ui.displayStatus(reordered),'partial');
  ui.applyEvent(reordered,{...ev(5,'after_tool','completed'),actor:'main:write_file'});
  assert.equal(ui.displayStatus(reordered),'completed');
  // All new choices are present in the real page, submitted and rendered in the timeline.
  const page = fs.readFileSync(path.join(__dirname, '../../api/static/index.html'), 'utf8');
  const extra = harness(), session = extra.ui.createSession('thread','新增技能','run');
  const choices = [['researchSkill','research','信息调研'],
    ['documentSkill','document-analysis','文档分析'], ['dataSkill','data-analysis','数据分析']];
  let seq = 0;
  for (const [id,skillId,label] of choices) {
    assert.ok(page.includes(`id="${id}"`));
    extra.element('#'+id).checked = true;
    for (const [state,caption] of [['selected','已选择'],['loaded','正文已读取'],['resource_loaded','参考资源已读取']]) {
      extra.ui.applyEvent(session,ev(++seq,'skill_state',state,{skill_id:skillId}));
      assert.ok(extra.element('#logArea').innerHTML.includes(`Skill ${label}：${caption}`));
    }
  }
  extra.element('#queryInput').value='调研资料、解读文档并分析数据';
  extra.context.reply=url=>response(url==='/api/task'?{thread_id:'thread',run_id:'run'}:
    url.includes('/history')?history():{files:[]});
  await extra.ui.sendTask();
  const submitted = JSON.parse(extra.calls.find(c=>c.url==='/api/task').options.body);
  assert.deepEqual(submitted.skill_ids,['research','document-analysis','data-analysis']);
  assert.equal(submitted.business_mode,'ordinary');
  console.log('PASS: persistence, refresh, full answers, ordered deduplication, Skill/review/refinement, redacted refinement reasons, partial and terminal states, stale HTTP, busy/validation, cancellation race, scoped downloads, escaping, view cleanup');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
