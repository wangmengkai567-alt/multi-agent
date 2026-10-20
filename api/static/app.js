/* Run views persist identities only; the server owns answers, events and status. */
const sessions = Object.create(null);
let activeSessionId = null;
let submitting = false;
let fileRequest = 0;
const storageKey = 'multiagent.runs.v1';
const $ = selector => document.querySelector(selector);
const activeStates = new Set(['queued', 'running']);
const statusNames = {queued:'等待执行', running:'执行中', completed:'已完成', failed:'执行失败',
  pending:'后台执行中', cancelled:'已取消', timed_out:'已超时', interrupted:'执行中断', partial:'部分完成', unknown:'状态待同步'};
const actors = {main:'任务协调助手', db:'数据库查询助手', search:'联网搜索助手'};
const skills = {'ecommerce-analysis':'运营分析', 'report-writing':'报告写作',
  'research':'信息调研', 'document-analysis':'文档分析', 'data-analysis':'数据分析'};
const verdicts = {passed:'通过', insufficient:'信息不足', dependency_unavailable:'依赖不可用',
  check_incomplete:'检查未完成', tool_failed:'工具失败', budget_exhausted:'次数耗尽', not_checked:'未检查'};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[c]));
}
// Preserve numeric ranges like 22~23 and require ~~ for strikethrough.
if (typeof marked !== 'undefined') marked.use({tokenizer:{del(src) {
  return src.startsWith('~~') ? false : undefined;
}}});
function renderAnswer(element, text) {
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    element.textContent = text;
    return;
  }
  element.innerHTML = DOMPurify.sanitize(marked.parse(String(text), {gfm:true, breaks:false}), {
    ALLOWED_TAGS:['p','br','hr','h1','h2','h3','h4','h5','h6','strong','em','del',
      'blockquote','ul','ol','li','pre','code','table','thead','tbody','tr','th','td','a'],
    ALLOWED_ATTR:['href','title','align','start'], ALLOW_DATA_ATTR:false,
    ALLOWED_URI_REGEXP:/^(?:https?:|mailto:|\/(?!\/)|#)/i
  });
  for (const link of element.querySelectorAll('a[href]')) {
    link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener noreferrer');
  }
}
function notify(message = '') { $('#notice').textContent = message; }
function persist() {
  try {
    localStorage.setItem(storageKey, JSON.stringify({active:activeSessionId,
      runs:Object.values(sessions).map(s => ({threadId:s.id, runId:s.runId, query:s.query}))}));
  } catch (_) { notify('浏览器无法保存任务列表；刷新后可通过运行详情恢复。'); }
}
async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data.status || data);
    throw Object.assign(new Error(detail), {status:response.status, data});
  }
  return data;
}
function createSession(threadId, query, runId, select = true) {
  if (sessions[runId]) { if (select) switchSession(runId); return sessions[runId]; }
  const session = {id:threadId, runId, query, logs:[], seen:new Set(), stream:null, ws:null,
    status:'unknown', statusSeq:0, answer:null, error:null, degraded:false, recordingIncomplete:false};
  sessions[runId] = session;
  if (select) switchSession(runId);
  persist();
  return session;
}
function applyEvent(session, event) {
  if (sessions[session.runId] !== session || event.run_id !== session.runId || event.thread_id !== session.id) return;
  if (!Number.isSafeInteger(event.event_seq) || event.event_seq < 1 || session.seen.has(event.event_seq)) return;
  session.seen.add(event.event_seq);
  session.logs.push(event);
  session.logs.sort((a,b) => a.event_seq - b.event_seq);
  const data = event.data || {};
  if (data.delivery_status) session.deliveryStatus = data.delivery_status;
  const lifecycle = ['run_queued','run_started','task_result','error','run_cancelled','run_timed_out','run_interrupted'];
  const currentState = lifecycle.includes(event.event) && event.event_seq > session.statusSeq;
  if (currentState) session.statusSeq = event.event_seq;
  if (event.event === 'session_created') session.dir = data.path;
  if (currentState && event.event === 'run_queued') session.status = 'queued';
  if (currentState && event.event === 'run_started') session.status = 'running';
  if (event.event === 'workflow_state' && ['degraded','completed_degraded'].includes(event.status)) session.degraded = true;
  if (currentState && event.event === 'task_result') {
    session.status = 'completed'; session.answer = data.result;
    if (data.jobs) { session.jobs = data.jobs; if (typeof setTimeout === 'function') syncJobs(session); }
    session.recordingIncomplete = data.recording_status === 'incomplete';
  }
  if (currentState && ['error','run_cancelled','run_timed_out','run_interrupted'].includes(event.event)) {
    session.status = event.status; session.error = data.error;
    if (data.partial_result) session.answer = data.partial_result;
  }
  if (!activeStates.has(session.status) && session.status !== 'unknown') {
    if (session.stream) session.stream.close();
    refreshFiles();
  }
  renderTabs();
  if (activeSessionId === session.runId) renderLog(session);
}
function displayStatus(session) {
  if (session.status==='completed' && session.deliveryStatus==='partial') return 'partial';
  if (session.status === 'completed' && session.jobs?.some(job=>job.status==='working'||job.status==='input_required')) return 'pending';
  if (session.status === 'completed' && session.jobs?.some(job=>job.status==='failed'||job.result?.partial)) return 'partial';
  // Recovered failures need not make a later, checked successful result partial.
  const latestChecks = new Map();
  const latestTasks = new Map();
  const latestTools = new Map();
  for (const e of session.logs) {
    if (['subagent_review','answer_validation'].includes(e.event)) latestChecks.set(e.call_id, e.status);
    if (['subagent_completed','subagent_failed','subagent_rejected'].includes(e.event)) latestTasks.set(e.call_id, e.status);
    if (e.event === 'after_tool' && e.data?.error_category !== 'search_limit') latestTools.set(e.call_id, e.status);
  }
  const finalCheck = session.logs.filter(e => e.event === 'answer_validation').at(-1);
  const completeAnswer = finalCheck?.status === 'passed' && finalCheck.data?.answer_kind === 'complete';
  const unchecked = !completeAnswer && ([...latestChecks.values()].some(v => v !== 'passed') ||
    [...latestTasks.values()].some(v => v !== 'completed') ||
    [...latestTools.values()].some(v => v !== 'completed'));
  return session.status === 'completed' && (session.degraded || unchecked || session.recordingIncomplete) ? 'partial' : session.status;
}
async function syncSession(session) {
  try {
    const history = await request('/api/runs/' + encodeURIComponent(session.runId) + '/history');
    if (sessions[session.runId] !== session) return;
    if (history.run.thread_id !== session.id || history.run.run_id !== session.runId) throw new Error('运行归属不匹配');
    session.subtasks = history.subtasks || [];
    for (const e of history.events) applyEvent(session, e);
    // A live terminal event may arrive while this earlier HTTP snapshot is in flight.
    if (activeStates.has(session.status) || session.status === 'unknown' || !activeStates.has(history.run.status)) {
      session.status = history.run.status;
      session.answer = history.run.answer;
      session.error = history.run.error;
    }
    session.syncError = '';
    if (activeStates.has(session.status) && !session.stream) {
      for (const other of Object.values(sessions)) {
        if (other !== session && other.id === session.id && other.stream) { other.stream.close(); other.stream = null; }
      }
      session.stream = new RunEventStream(session.id, session.runId, e => applyEvent(session, e), socket => {
        session.ws = socket; updateWSStatus();
      });
      // These events are already applied, so reconnect from the last contiguous event.
      for (const e of session.logs) session.stream.seen.add(session.runId + ':' + e.event_seq);
      while (session.seen.has(session.stream.afterSeq + 1)) session.stream.afterSeq++;
      session.stream.connect();
    }
  } catch (error) {
    session.syncError = error.status === 404 ? '服务端找不到该轮记录；可移除此视图。' : '读取记录失败：' + error.message;
  }
  renderTabs();
  if (activeSessionId === session.runId) renderLog(session);
  if (typeof setTimeout === 'function') syncJobs(session);
}
function switchSession(runId) {
  activeSessionId = runId;
  renderTabs(); renderLog(sessions[runId]); renderJobs(sessions[runId]); persist(); refreshFiles(); updateWSStatus();
}
function closeSession(runId) {
  const session = sessions[runId];
  if (!session) return;
  if (session.stream) session.stream.close();
  if (session.jobTimer && typeof clearTimeout === 'function') clearTimeout(session.jobTimer);
  delete sessions[runId];
  if (activeSessionId === runId) activeSessionId = Object.keys(sessions).at(-1) || null;
  renderTabs(); renderLog(sessions[activeSessionId]); persist(); refreshFiles(); updateWSStatus();
  notify('已移除浏览器视图；运行与历史记录仍保存在服务端。');
}
function closeAllSessions() { Object.keys(sessions).forEach(closeSession); }
function renderTabs() {
  $('#tabBar').innerHTML = Object.values(sessions).map(s => {
    const state = displayStatus(s);
    return `<div class="tab${s.runId === activeSessionId ? ' active' : ''}">
      <button class="btn btn-ghost" data-open="${escapeHtml(s.runId)}">
        <span class="tab-status ${escapeHtml(state)}">${statusNames[state] || '状态待同步'}</span>
        <span class="tab-query">${escapeHtml(s.query.slice(0,24))}</span></button>
      <button class="btn btn-ghost" data-close="${escapeHtml(s.runId)}" aria-label="移除此任务视图">×</button></div>`;
  }).join('');
  const select = $('#threadSelect'), selected = select.value;
  const threads = [...new Map(Object.values(sessions).map(s => [s.id,s])).values()];
  select.innerHTML = '<option value="">新会话</option>' + threads.map((s,i) =>
    `<option value="${escapeHtml(s.id)}">会话 ${i+1} · ${escapeHtml(s.query.slice(0,18))}</option>`).join('');
  if (threads.some(s => s.id === selected)) select.value = selected;
}
function describeEvent(event) {
  const d = event.data || {}, who = d.assistant_name || actors[event.actor] || '助手';
  if (d.error_category === 'search_limit') return event.event === 'on_error'
    ? '检索额度限制：已拦截新的请求，已获准请求仍会完成'
    : '此检索请求未执行（检索额度限制）';
  if (event.event === 'review_repair') return '检查输出格式不符合约定：仅重试检查，复用已有依据';
  if (event.event === 'skill_state') return `Skill ${skills[d.skill_id] || '任务说明'}：` +
    ({selected:'已选择', loaded:'正文已读取', resource_loaded:'参考资源已读取'}[event.status] || event.status);
  if (event.event === 'answer_validation') return '最终答复检查：' + (event.status === 'partial' ? '有限结果' : verdicts[event.status] || event.status) + (d.issues?.length ? '；' + d.issues.join('；') : '');
  if (event.event === 'subagent_review') return `${who}结果检查：${verdicts[event.status] || event.status}` +
    (d.issues?.length ? '；' + d.issues.join('；') : '');
  if (event.event === 'subagent_refinement') {
    const revision = d.revision_instruction;
    const reason = revision && !['[REDACTED]','[OMITTED]'].includes(revision) ? revision : d.issues?.join('；');
    return `${who}补充查询：${reason || '补齐缺失依据'}`;
  }
  if (event.event === 'budget_finalizing') return '预算接近阈值：优先交付已核实部分';
  if (event.event === 'tool_retry') return `运行时重试 #${d.attempt}：等待 ${Number(d.delay_seconds || 0).toFixed(2)} 秒`;
  if (event.event === 'research_plan') return '研究分工已登记：' + (d.tasks || []).map(t => t.label).join('、');
  if (event.event === 'search_finalizing') return '检索修订已结束：汇总已核实内容和剩余缺口';
  if (event.status === 'cancelled' && ['on_error','after_tool'].includes(event.event)) {
    const reason = d.error && !/^<object object at 0x[0-9a-f]+>$/i.test(d.error) ? d.error : '当前操作已取消';
    return '操作已取消：' + reason;
  }
  if (event.event === 'after_tool') return (d.tool_name || event.actor?.split(':').at(-1) || '工具') + (event.status === 'completed' ? ' · 已完成' : ' · 未完成：' + (d.error || event.status));
  if (event.event === 'on_error') return '动作失败：' + (d.error || '请查看详情');
  if (event.event === 'workflow_state') return ({initialized:'分析流程已准备', blocked:'前置条件未满足，需先完成查询或文件读取',
    degraded:'数据库不可用，改为外部资料分析', search_allowed:'内部查询已通过，开始市场检索',
    completed_degraded:'仅完成外部资料分析，缺少内部经营指标', completed:'所需查询与文件检查已完成',
    report_allowed:'报告前置条件已通过', upload_read:'用户文件已读取'}[event.status] || '分析流程更新');
  if (event.event === 'result_handoff') return '已将本轮查询摘要传给后续助手';
  if (event.event === 'before_tool') return '执行前检查 · ' + (d.tool_name || event.actor?.split(':').at(-1) || '工具');
  if (event.event === 'before_delegate') return `${who}执行前检查`;
  if (event.event === 'after_subagent') return `${who}处理结束：${verdicts[d.check_status] || event.status}`;
  if (event.event === 'before_run') return '准备本轮任务与限制';
  if (event.event === 'after_run') return '本轮执行与清理结束';
  if (event.event === 'task_result') return '本轮结果已保存';
  if (event.event === 'error') return '本轮执行失败：' + (d.error || '请查看详情');
  if (event.event.startsWith('run_')) return ({run_queued:'任务已接收',run_started:'任务开始执行',
    run_cancelled:'任务已取消',run_timed_out:'任务超过总时限',run_interrupted:'服务中断了本轮执行'}[event.event] || '运行状态更新');
  return event.message || '执行进度更新';
}
function renderLog(session) {
  const answer = $('#answer');
  if (!session) {
    $('#runSummary').textContent = '选择任务可查看进度和结果'; answer.hidden = true; answer.textContent = '';
    $('#logArea').innerHTML = '<div class="empty-state">输入问题并发送任务</div>'; if ($('#jobArea')) $('#jobArea').innerHTML = ''; return;
  }
  const state = displayStatus(session);
  const partialWork = !activeStates.has(session.status) && session.status !== 'completed' && session.logs.some(e => e.event === 'subagent_completed');
  $('#runSummary').innerHTML = `<strong>${statusNames[state] || '状态待同步'}</strong>${partialWork ? ' · 已有部分助手结果，见执行记录' : ''}
    <button class="btn btn-ghost" id="cancelBtn" ${activeStates.has(session.status) ? '' : 'disabled'}>取消本轮</button>
    <button class="btn btn-ghost" id="syncBtn">重新读取记录</button>
    ${session.error ? `<p>${escapeHtml(session.error)}</p>` : ''}
    ${session.syncError ? `<p>${escapeHtml(session.syncError)}</p>` : ''}
    ${session.recordingIncomplete ? '<p>部分执行记录未保存，详情见最终事件。</p>' : ''}
    <details><summary>运行详情</summary><pre>${escapeHtml(JSON.stringify({thread_id:session.id,run_id:session.runId,status:session.status},null,2))}</pre></details>`;
  renderJobs(session);
  answer.hidden = session.answer === null || session.answer === undefined;
  renderAnswer(answer, session.answer ?? '');
  const area = $('#logArea');
  const follow = area.scrollHeight - area.scrollTop - area.clientHeight < 80;
  const openDetails = new Set([...area.querySelectorAll('details[open]')].map(d => d.dataset.seq));
  area.innerHTML = renderCallGroups(session, openDetails) || '<div class="empty-state">等待执行记录…</div>';
  if (follow) area.scrollTop = area.scrollHeight;
}
function callGroups(session) {
  const agents = new Map(), aliases = new Map(), labels = new Map();
  for (const event of session.logs) {
    if (event.event === 'research_plan') for (const task of event.data?.tasks || []) labels.set(task.task_id, task.label);
    if (event.event.startsWith('subagent_') && !['subagent_review','subagent_refinement'].includes(event.event)) {
      const d = event.data || {};
      if (!agents.has(event.call_id)) agents.set(event.call_id, {id:event.call_id, target:d.target || event.actor, label:d.task_label || '', events:[]});
      if (d.request_call_id) aliases.set(d.request_call_id,event.call_id);
      if (d.task_label) agents.get(event.call_id).label = d.task_label;
    }
  }
  for (const task of session.subtasks || []) {
    if (!agents.has(task.call_id)) agents.set(task.call_id,{id:task.call_id,target:task.target,label:'',events:[]});
    if (task.request_call_id) aliases.set(task.request_call_id,task.call_id);
  }
  const root = {id:session.runId,target:'main',label:'主助手',events:[]};
  const owners = new Map(session.logs.map(e => [e.call_id,e.parent_call_id]));
  function owner(event) {
    // Older providers may reuse a tool ID across agents; the event's own parent wins.
    if (agents.has(event.parent_call_id)) return agents.get(event.parent_call_id);
    let id = event.call_id;
    const visited = new Set();
    while (id && !visited.has(id)) {
      visited.add(id);
      id = aliases.get(id) || id;
      if (agents.has(id)) return agents.get(id);
      id = owners.get(id);
    }
    return root;
  }
  for (const group of agents.values()) group.requestIds = new Set([...aliases].filter(([,id])=>id===group.id).map(([id])=>id));
  for (const event of session.logs) {
    const group = owner(event);
    group.events.push(event);
    if (event.data?.task_label) group.label = event.data.task_label;
  }
  return [root,...agents.values()].filter(group => group.events.length);
}
function renderCallGroups(session, openDetails = new Set()) {
  return callGroups(session).map((group,index) => {
    const agentName = actors[group.target] || group.target || '助手';
    const terminal = group.events.filter(e=>['subagent_completed','subagent_failed','subagent_cancelled','subagent_rejected'].includes(e.event)).at(-1);
    const outcome = terminal ? ' · ' + ({completed:'检查通过',failed:'未通过',cancelled:'已取消',rejected:'已拒绝'}[terminal.status] || terminal.status) : '';
    const title = index === 0 && group.target === 'main' ? '主助手' : `${agentName} · ${group.label || '独立分工'} · ${group.id.slice(0,8)}${outcome}`;
    const calls = new Map();
    for (const e of group.events) {
      const key = group.requestIds?.has(e.call_id) ? group.id : e.call_id;
      if (!calls.has(key)) calls.set(key,[]);
      calls.get(key).push(e);
    }
    let toolNumber = 0;
    const rows = [...calls.values()].map((events,i) => {
      const last = events.at(-1), d = last.data || {};
      const failed = d.error_category !== 'search_limit' && ['failed','rejected','dependency_unavailable','insufficient','check_incomplete'].includes(last.status);
      const name = events.find(e=>e.data?.tool_name)?.data.tool_name || events.find(e=>e.actor?.includes(':'))?.actor.split(':').at(-1);
      const toolNames = {internet_search:'网络搜索',read_web_page:'网页正文读取',read_file:'读取文件',write_todos:'更新计划',plan_research:'登记分工',task:'委派任务'};
      const caption = name ? `${toolNames[name] || name} #${++toolNumber} · ${last.event==='after_tool' && last.status==='completed'?'已完成':describeEvent(last)}` : describeEvent(last);
      const budget = [...events].reverse().find(e=>e.data?.search_budget)?.data.search_budget;
      const quota = budget ? ` · 本任务 ${budget.task_used}/${budget.task_limit}，整轮 ${budget.run_used}/${budget.run_limit}` : '';
      const start = new Date(events[0].timestamp), end = new Date(last.timestamp);
      const elapsed = Number.isFinite(end-start) ? ` · ${((end-start)/1000).toFixed(1)} 秒` : '';
      return `<div class="log-entry" data-call="${escapeHtml(last.call_id)}"><div class="log-dot ${failed?'e':'a'}"></div><div class="log-body">
        <span class="log-msg">${escapeHtml(caption + quota + elapsed)}</span>
        <div class="log-time">${escapeHtml(end.toLocaleTimeString('zh-CN',{hour12:false}))}</div>
        <details data-seq="${escapeHtml('call:'+last.call_id)}" ${openDetails.has('call:'+last.call_id)?'open':''}><summary>展开调用过程（${events.length} 条）</summary>
        ${events.map(e=>`<div>${escapeHtml(describeEvent(e))}</div>`).join('')}
        <pre>${escapeHtml(JSON.stringify(events,null,2))}</pre></details></div></div>`;
    }).join('');
    const key = 'group:'+group.id;
    return `<details class="agent-group" data-agent="${escapeHtml(group.id)}" data-seq="${escapeHtml(key)}" ${openDetails.has(key)?'open':''}>
      <summary>${escapeHtml(title)} · ${toolNumber} 次工具调用</summary>${rows}</details>`;
  }).join('');
}
async function syncJobs(session) {
  if (session.jobSyncing || sessions[session.runId] !== session) return;
  session.jobSyncing = true;
  try {
    const data = await request('/api/runs/'+encodeURIComponent(session.runId)+'/jobs');
    if (sessions[session.runId] !== session) return;
    session.jobs = data.jobs || [];
    session.jobError = '';
    renderTabs();
    renderJobs(session);
    const working = session.jobs.filter(j=>j.status === 'working');
    if (session.jobs.some(job=>job.kind==='pdf_conversion' && job.status==='completed')) refreshFiles();
    if (working.length && typeof setTimeout === 'function') {
      clearTimeout(session.jobTimer);
      const delay = Math.max(1,...working.map(j=>j.poll_after_seconds || 3));
      session.jobTimer = setTimeout(()=>syncJobs(session),delay*1000);
    }
  } catch (error) {
    session.jobError = '后台任务状态读取失败：'+error.message;
    renderJobs(session);
    if (session.jobs?.some(j=>j.status==='working') && typeof setTimeout === 'function') {
      session.jobTimer = setTimeout(()=>syncJobs(session),5000);
    }
  } finally { session.jobSyncing = false; }
}
function renderJobs(session) {
  if (activeSessionId !== session.runId) return;
  const area = $('#jobArea');
  if (!area) return;
  const names = {working:'后台执行中',completed:'已完成',failed:'失败',cancelled:'已取消',input_required:'等待你的输入'};
  area.innerHTML = (session.jobs || []).map(job=>`<section class="agent-group" data-job="${escapeHtml(job.task_id)}">
    <h3>${job.kind==='pdf_conversion'?'PDF 转换':'研究任务'} · ${escapeHtml(job.task_id.slice(0,8))} · ${escapeHtml(names[job.status] || job.status)}${job.result?.partial?' · 有限结果':''}</h3>
    ${job.error?`<p>${escapeHtml(job.error)}</p>`:''}
    ${job.status==='working'?`<button class="btn" data-cancel-job="${escapeHtml(job.task_id)}">取消后台任务</button>`:''}
    ${job.status==='input_required'?`<label>补充信息 <input data-job-input="${escapeHtml(job.task_id)}"></label><button class="btn" data-resume-job="${escapeHtml(job.task_id)}">提交并继续</button>`:''}
    ${job.result?.answer?`<div data-job-answer="${escapeHtml(job.task_id)}"></div>`:''}
    ${(job.result?.artifacts || []).map(file=>`<a class="btn" href="/api/download?path=${encodeURIComponent(file.path)}" target="_blank" rel="noopener">下载 ${escapeHtml(file.name)}</a>`).join('')}
    ${job.execution_run_id?`<button class="btn" data-open-job="${escapeHtml(job.execution_run_id)}" data-job-thread="${escapeHtml(job.task_id)}">查看后台执行详情</button>`:''}
    </section>`).join('') + (session.jobError?`<p>${escapeHtml(session.jobError)}</p>`:'');
  for (const job of session.jobs || []) if (job.result?.answer) renderAnswer(area.querySelector(`[data-job-answer="${job.task_id}"]`),job.result.answer);
}
async function sendTask() {
  const input = $('#queryInput'), query = input.value.trim();
  if (!query || submitting) return;
  submitting = true; $('#sendBtn').disabled = true; notify('正在提交…');
  const skillIds = [];
  if ($('#ecommerceSkill').checked) skillIds.push('ecommerce-analysis');
  if ($('#reportSkill').checked) skillIds.push('report-writing');
  if ($('#researchSkill').checked) skillIds.push('research');
  if ($('#documentSkill').checked) skillIds.push('document-analysis');
  if ($('#dataSkill').checked) skillIds.push('data-analysis');
  const mode = $('#modeSelect').value;
  if (mode === 'internal_external' && !skillIds.includes('ecommerce-analysis')) {
    skillIds.push('ecommerce-analysis'); $('#ecommerceSkill').checked = true;
  }
  try {
    const data = await request('/api/task', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({query, execution_mode:$('#executionSelect').value || 'auto', skill_ids:skillIds, business_mode:mode, thread_id:$('#threadSelect').value || null})});
    const session = createSession(data.thread_id, query, data.run_id);
    session.status = 'queued'; input.value = ''; notify('任务已接收');
    await syncSession(session);
  } catch (error) {
    if (error.status === 409) {
      const session = createSession(error.data.thread_id, '该会话正在执行的任务', error.data.run_id);
      notify('该会话忙碌，已显示正在执行的任务。请等待、取消，或选择新会话。');
      await syncSession(session);
    } else notify('提交失败：' + error.message);
  } finally { submitting = false; $('#sendBtn').disabled = false; }
}
async function cancelRun() {
  const session = sessions[activeSessionId];
  if (!session || !activeStates.has(session.status)) return;
  $('#cancelBtn').disabled = true;
  try {
    const record = await request('/api/runs/' + encodeURIComponent(session.runId) + '/cancel', {method:'POST'});
    session.status = record.status; session.answer = record.answer; session.error = record.error;
    if (session.stream) { session.stream.close(); session.stream = null; }
    await syncSession(session); notify('当前状态：' + (statusNames[displayStatus(session)] || record.status));
  } catch (error) { notify('取消请求失败：' + error.message); renderLog(session); }
}
function updateWSStatus() {
  const current = sessions[activeSessionId];
  const connected = current?.ws?.readyState === WebSocket.OPEN;
  $('#wsDot').className = 'ws-dot' + (connected ? '' : ' off');
  $('#wsStatus').textContent = !current ? '就绪' : activeStates.has(current.status) ?
    (connected ? '实时进度已连接' : '连接中断，正在重连') : '查看已保存记录';
}
async function refreshFiles() {
  const version = ++fileRequest, session = sessions[activeSessionId], list = $('#fileList');
  if (!session) { list.innerHTML = '<div class="empty-state">选择任务后查看本轮文件</div>'; return; }
  try {
    const path = `session_${session.id}/run_${session.runId}`;
    const data = await request('/api/files?path=' + encodeURIComponent(path));
    if (version !== fileRequest) return;
    list.innerHTML = data.files.map(f => `<div class="file-item"><div class="file-info">
      <div class="file-name">${escapeHtml(f.name)}</div><div class="file-meta">${f.size} B</div></div>
      <a class="btn btn-primary" href="/api/download?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener">下载</a></div>`).join('') || '<div class="empty-state">本轮暂无文件</div>';
  } catch (error) {
    if (version === fileRequest) list.textContent = error.status === 404 ? '本轮暂无文件' : '文件读取失败：' + error.message;
  }
}
async function restoreSessions() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; }
  catch (_) { notify('浏览器任务列表损坏，请重新发送任务。'); saved = {}; }
  const validId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
  for (const entry of Array.isArray(saved.runs) ? saved.runs : []) {
    if (validId(entry.threadId) && validId(entry.runId) && typeof entry.query === 'string') createSession(entry.threadId,entry.query,entry.runId,false);
  }
  activeSessionId = sessions[saved.active] ? saved.active : Object.keys(sessions).at(-1) || null;
  renderTabs(); renderLog(sessions[activeSessionId]);
  await Promise.all(Object.values(sessions).map(syncSession));
  persist(); refreshFiles(); updateWSStatus();
}
$('#tabBar').addEventListener('click', e => {
  const open = e.target.closest('[data-open]'), close = e.target.closest('[data-close]');
  if (open) switchSession(open.dataset.open);
  if (close) closeSession(close.dataset.close);
});
$('#runSummary').addEventListener('click', e => {
  if (e.target.id === 'cancelBtn') cancelRun();
  if (e.target.id === 'syncBtn' && sessions[activeSessionId]) syncSession(sessions[activeSessionId]);
});
$('#modeSelect').addEventListener('change', () => {
  if ($('#modeSelect').value === 'internal_external') $('#ecommerceSkill').checked = true;
});
$('#queryInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTask(); } });
restoreSessions();

if ($('#jobArea')) $('#jobArea').addEventListener('click', async event => {
  const session = sessions[activeSessionId];
  if (!session) return;
  const target = event.target;
  try {
    if (target.dataset.cancelJob) await request('/api/jobs/'+encodeURIComponent(target.dataset.cancelJob)+'/cancel?thread_id='+encodeURIComponent(session.id),{method:'POST'});
    if (target.dataset.resumeJob) {
      const id = target.dataset.resumeJob;
      const input = $('#jobArea').querySelector(`[data-job-input="${id}"]`).value;
      await request('/api/jobs/'+encodeURIComponent(id)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({thread_id:session.id,user_input:input})});
    }
    if (target.dataset.openJob) {
      const view = createSession(target.dataset.jobThread,'后台研究执行',target.dataset.openJob);
      await syncSession(view);
      return;
    }
    await syncJobs(session);
  } catch (error) { notify(error.message); }
});
