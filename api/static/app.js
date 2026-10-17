/* Run views persist identities only; the server owns answers, events and status. */
const sessions = Object.create(null);
let activeSessionId = null;
let submitting = false;
let fileRequest = 0;
const storageKey = 'multiagent.runs.v1';
const $ = selector => document.querySelector(selector);
const activeStates = new Set(['queued', 'running']);
const statusNames = {queued:'等待执行', running:'执行中', completed:'已完成', failed:'执行失败',
  cancelled:'已取消', timed_out:'已超时', interrupted:'执行中断', partial:'部分完成', unknown:'状态待同步'};
const actors = {main:'主助手', db:'数据分析助手', search:'市场情报助手', knowledge:'内部知识助手'};
const skills = {'ecommerce-analysis':'运营分析', 'report-writing':'报告写作'};
const verdicts = {passed:'通过', insufficient:'信息不足', dependency_unavailable:'依赖不可用',
  check_incomplete:'检查未完成', tool_failed:'工具失败', budget_exhausted:'次数耗尽', not_checked:'未检查'};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[c]));
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
  const lifecycle = ['run_queued','run_started','task_result','error','run_cancelled','run_timed_out','run_interrupted'];
  const currentState = lifecycle.includes(event.event) && event.event_seq > session.statusSeq;
  if (currentState) session.statusSeq = event.event_seq;
  if (event.event === 'session_created') session.dir = data.path;
  if (currentState && event.event === 'run_queued') session.status = 'queued';
  if (currentState && event.event === 'run_started') session.status = 'running';
  if (event.event === 'workflow_state' && ['degraded','completed_degraded'].includes(event.status)) session.degraded = true;
  if (currentState && event.event === 'task_result') {
    session.status = 'completed'; session.answer = data.result;
    session.recordingIncomplete = data.recording_status === 'incomplete';
  }
  if (currentState && ['error','run_cancelled','run_timed_out','run_interrupted'].includes(event.event)) {
    session.status = event.status; session.error = data.error;
  }
  if (!activeStates.has(session.status) && session.status !== 'unknown') {
    if (session.stream) session.stream.close();
    refreshFiles();
  }
  renderTabs();
  if (activeSessionId === session.runId) renderLog(session);
}
function displayStatus(session) {
  // Recovered failures need not make a later, checked successful result partial.
  const latestChecks = new Map();
  const latestTasks = new Map();
  const latestTools = new Map();
  for (const e of session.logs) {
    if (e.event === 'subagent_review') latestChecks.set(e.call_id, e.status);
    if (['subagent_completed','subagent_failed','subagent_rejected'].includes(e.event)) latestTasks.set(e.actor, e.status);
    if (e.event === 'after_tool') latestTools.set(e.actor, e.status);
  }
  const unchecked = [...latestChecks.values()].some(v => v !== 'passed') ||
    [...latestTasks.values()].some(v => v !== 'completed') ||
    [...latestTools.values()].some(v => v !== 'completed');
  return session.status === 'completed' && (session.degraded || unchecked || session.recordingIncomplete) ? 'partial' : session.status;
}
async function syncSession(session) {
  try {
    const history = await request('/api/runs/' + encodeURIComponent(session.runId) + '/history');
    if (sessions[session.runId] !== session) return;
    if (history.run.thread_id !== session.id || history.run.run_id !== session.runId) throw new Error('运行归属不匹配');
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
}
function switchSession(runId) {
  activeSessionId = runId;
  renderTabs(); renderLog(sessions[runId]); persist(); refreshFiles(); updateWSStatus();
}
function closeSession(runId) {
  const session = sessions[runId];
  if (!session) return;
  if (session.stream) session.stream.close();
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
  if (event.event === 'skill_state') return `Skill ${skills[d.skill_id] || '任务说明'}：` +
    ({selected:'已选择', loaded:'正文已读取', resource_loaded:'参考资源已读取'}[event.status] || event.status);
  if (event.event === 'subagent_review') return `${who}结果检查：${verdicts[event.status] || event.status}` +
    (d.issues?.length ? '；' + d.issues.join('；') : '');
  if (event.event === 'subagent_refinement') {
    const revision = d.revision_instruction;
    const reason = revision && !['[REDACTED]','[OMITTED]'].includes(revision) ? revision : d.issues?.join('；');
    return `${who}补充查询：${reason || '补齐缺失依据'}`;
  }
  if (event.event === 'after_tool') return event.status === 'completed' ? '工具执行完成' : '工具未完成：' + (d.error || event.status);
  if (event.event === 'on_error') return '动作失败：' + (d.error || '请查看详情');
  if (event.event === 'workflow_state') return ({initialized:'分析流程已准备', blocked:'前置条件未满足，需先完成查询或文件读取',
    degraded:'数据库不可用，改为外部资料分析', search_allowed:'内部查询已通过，开始市场检索',
    completed_degraded:'仅完成外部资料分析，缺少内部经营指标', completed:'所需查询与文件检查已完成',
    report_allowed:'报告前置条件已通过', upload_read:'用户文件已读取'}[event.status] || '分析流程更新');
  if (event.event === 'result_handoff') return '已将本轮查询摘要传给后续助手';
  if (event.event === 'before_tool') return '工具执行前检查';
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
    $('#logArea').innerHTML = '<div class="empty-state">输入问题并发送任务</div>'; return;
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
  answer.hidden = session.answer === null || session.answer === undefined;
  answer.textContent = session.answer ?? '';
  const area = $('#logArea');
  const follow = area.scrollHeight - area.scrollTop - area.clientHeight < 80;
  const openDetails = new Set([...area.querySelectorAll('details[open]')].map(d => d.dataset.seq));
  area.innerHTML = session.logs.map(e => {
    const failed = ['failed','rejected','dependency_unavailable','insufficient','check_incomplete'].includes(e.status);
    return `<div class="log-entry"><div class="log-dot ${failed ? 'e':'a'}"></div><div class="log-body">
      <span class="log-msg">${escapeHtml(describeEvent(e))}</span>
      <div class="log-time">${escapeHtml(new Date(e.timestamp).toLocaleTimeString('zh-CN',{hour12:false}))}</div>
      <details data-seq="${e.event_seq}" ${openDetails.has(String(e.event_seq)) ? 'open':''}><summary>展开执行详情</summary><pre>${escapeHtml(JSON.stringify(e,null,2))}</pre></details></div></div>`;
  }).join('') || '<div class="empty-state">等待执行记录…</div>';
  if (follow) area.scrollTop = area.scrollHeight;
}
async function sendTask() {
  const input = $('#queryInput'), query = input.value.trim();
  if (!query || submitting) return;
  submitting = true; $('#sendBtn').disabled = true; notify('正在提交…');
  const skillIds = [];
  if ($('#ecommerceSkill').checked) skillIds.push('ecommerce-analysis');
  if ($('#reportSkill').checked) skillIds.push('report-writing');
  const mode = $('#modeSelect').value;
  if (mode === 'internal_external' && !skillIds.includes('ecommerce-analysis')) {
    skillIds.push('ecommerce-analysis'); $('#ecommerceSkill').checked = true;
  }
  try {
    const data = await request('/api/task', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({query, skill_ids:skillIds, business_mode:mode, thread_id:$('#threadSelect').value || null})});
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
