/* Run views persist identities only; the server owns answers, events and status. */
const sessions = Object.create(null);
let activeSessionId = null;
let submitting = false;
let fileRequest = 0;
let authenticated = false;
let csrfToken = null;
let authEpoch = 0;
let authTimer = null;
const authChannel = new BroadcastChannel('multiagent.auth.v1');
let authBootstrap = 0;
const storageKey = 'multiagent.runs.v1';
const $ = selector => document.querySelector(selector);
const activeStates = new Set(['queued', 'running']);
const statusNames = {queued:'等待执行', running:'执行中', completed:'已完成', failed:'执行失败',
  rejected:'已拒绝', cancelled:'已取消', timed_out:'已超时', interrupted:'执行中断', partial:'部分完成', unknown:'状态待同步', coverage_unknown:'完成情况待确认'};
const actors = {main:'任务协调助手', db:'数据库查询助手', search:'联网搜索助手', document:'材料分析助手', delivery:'文件交付'};
const skills = Object.create(null);
let skillCatalog = null;


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
  if (!authenticated) return;
  try {
    localStorage.setItem(storageKey, JSON.stringify({active:activeSessionId,
      runs:Object.values(sessions).map(s => ({threadId:s.id, runId:s.runId, query:s.query}))}));
  } catch (_) { notify('浏览器无法保存任务列表；刷新后可通过运行详情恢复。'); }
}
function lockAuthentication(message = '请登录后继续。已提交的任务仍会运行。') {
  authenticated = false; csrfToken = null; authEpoch++; fileRequest++;
  clearTimeout(authTimer); authTimer = null;
  for (const session of Object.values(sessions)) {
    session.stream?.close();
    delete sessions[session.runId];
  }
  activeSessionId = null; skillCatalog = null; submitting = false;
  $('#workspace').hidden = true; $('#loginPanel').hidden = false; $('#logoutBtn').hidden = true;
  $('#authMessage').textContent = message;
  $('#queryInput').value = ''; $('#inputText').value = ''; $('#sendBtn').disabled = true;
  $('#answer').textContent = ''; $('#logArea').textContent = '';
  $('#tabBar').textContent = ''; $('#runSummary').textContent = ''; $('#fileList').textContent = '';
  $('#skillChoices').textContent = '登录后加载技能目录';
  for (const id of Object.keys(skills)) delete skills[id];
  $('#threadSelect').innerHTML = '<option value="">新会话</option>'; notify(''); updateWSStatus();
}
async function request(url, options = {}) {
  if (!authenticated) throw Object.assign(new Error('请先登录'), {status:401});
  const epoch = authEpoch;
  const headers = new Headers(options.headers || {});
  if (!['GET','HEAD','OPTIONS'].includes((options.method || 'GET').toUpperCase())) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(url, {...options, headers, credentials:'same-origin', cache:'no-store'});
  if (epoch !== authEpoch) throw new Error('会话已切换，已忽略旧请求');
  if (response.status === 401) {
    lockAuthentication('登录已失效，请重新登录。后台任务仍会运行。');
    throw Object.assign(new Error('登录已失效'), {status:401});
  }
  const data = await response.json();
  if (epoch !== authEpoch) throw new Error('会话已切换，已忽略旧请求');
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data.status || data);
    throw Object.assign(new Error(detail), {status:response.status, data});
  }
  return data;
}
async function checkAuthentication() {
  if (!authenticated) return false;
  const epoch = authEpoch;
  const response = await fetch('/api/auth/session', {credentials:'same-origin',cache:'no-store'});
  if (epoch !== authEpoch) return false;
  if (response.status === 401) { lockAuthentication('登录已失效，请重新登录。后台任务仍会运行。'); return false; }
  if (!response.ok) throw new Error('无法确认登录状态');
  return true;
}
async function enterWorkspace(data) {
  csrfToken = data.csrf_token; authenticated = true; authEpoch++;
  clearTimeout(authTimer);
  authTimer = setTimeout(() => lockAuthentication('登录已到期，请重新登录。后台任务仍会运行。'), Math.max(0, data.expires_at * 1000 - Date.now()));
  $('#loginPanel').hidden = true; $('#workspace').hidden = false; $('#logoutBtn').hidden = false;
  await Promise.all([loadSkillCatalog(), restoreSessions()]);
}
async function initializeAuthentication() {
  lockAuthentication('正在确认登录状态…');
  const attempt = ++authBootstrap;
  $('#loginBtn').disabled = true;
  try {
    const response = await fetch('/api/auth/session', {credentials:'same-origin',cache:'no-store'});
    if (attempt !== authBootstrap) return;
    if (response.status === 401) { $('#authMessage').textContent = '请输入访问口令。'; return; }
    if (!response.ok) throw new Error('读取登录状态失败');
    const data = await response.json();
    if (attempt !== authBootstrap) return;
    await enterWorkspace(data);
  } catch (error) { if (attempt === authBootstrap) $('#authMessage').textContent = error.message; }
  finally { if (attempt === authBootstrap) $('#loginBtn').disabled = false; }
}
$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#loginBtn'), input = $('#accessPassword');
  button.disabled = true; $('#authMessage').textContent = '正在登录…';
  try {
    const pending = fetch('/api/auth/login', {method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/json','X-Login-Intent':'login'}, body:JSON.stringify({password:input.value})});
    input.value = '';
    const response = await pending, data = await response.json();
    if (!response.ok) throw new Error(data.detail || '登录失败');
    authChannel.postMessage({type:'changed'});
    await enterWorkspace(data);
  } catch (error) { $('#authMessage').textContent = error.message; }
  finally { input.value = ''; button.disabled = false; }
});
$('#logoutBtn').addEventListener('click', async () => {
  $('#logoutBtn').disabled = true;
  try { await request('/api/auth/logout', {method:'POST'}); authChannel.postMessage({type:'logged_out'}); lockAuthentication('已退出登录。后台任务仍会运行。'); }
  catch (error) { if (authenticated) notify('退出失败：' + error.message); }
  finally { $('#logoutBtn').disabled = false; }
});
authChannel.onmessage = event => {
  if (event.data?.type === 'logged_out') { authBootstrap++; lockAuthentication('已在其他页面退出登录。后台任务仍会运行。'); $('#loginBtn').disabled = false; }
  if (event.data?.type === 'changed') initializeAuthentication().catch(error => { $('#authMessage').textContent = error.message; });
};
function requiredSkillsForMode() {
  const mode = $('#modeSelect').value;
  if (!Object.hasOwn(skillCatalog.required_skills, mode)) throw new Error('当前业务模式不在服务端技能目录中');
  return new Set(skillCatalog.required_skills[mode]);
}
function applyRequiredSkills() {
  if (skillCatalog === null) return;
  const required = requiredSkillsForMode();
  for (const input of $('#skillChoices').querySelectorAll('input[name="skill_id"]')) {
    input.disabled = required.has(input.value);
    input.title = input.disabled ? '当前业务模式要求选择此技能' : '';
    if (input.disabled) input.checked = true;
  }
}
async function loadSkillCatalog() {
  const epoch = authEpoch;
  const retry = $('#retrySkillsBtn'), choices = $('#skillChoices');
  retry.disabled = true; $('#sendBtn').disabled = true;
  choices.setAttribute('aria-busy', 'true');
  try {
    const catalog = await request('/api/skills');
    if (!authenticated) return;
    if (!catalog || !Array.isArray(catalog.skills) || catalog.skills.length === 0 ||
        catalog.max_selected !== catalog.skills.length || !catalog.required_skills ||
        typeof catalog.required_skills !== 'object' || Array.isArray(catalog.required_skills)) {
      throw new Error('服务端技能目录格式不符合约定');
    }
    const ids = new Set();
    for (const item of catalog.skills) {
      if (!item || typeof item.id !== 'string' || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(item.id) ||
          ids.has(item.id) || typeof item.label !== 'string' || !item.label.trim()) {
        throw new Error('服务端技能目录包含无效或重复的技能');
      }
      ids.add(item.id);
    }
    for (const option of $('#modeSelect').options) {
      if (!Object.hasOwn(catalog.required_skills, option.value)) throw new Error('服务端缺少当前业务模式的技能要求');
    }
    for (const required of Object.values(catalog.required_skills)) {
      if (!Array.isArray(required) || new Set(required).size !== required.length ||
          required.some(id => !ids.has(id))) throw new Error('服务端业务模式技能要求不符合约定');
    }
    choices.innerHTML = catalog.skills.map(item => `<label><input type="checkbox" name="skill_id"
      value="${escapeHtml(item.id)}"> ${escapeHtml(item.label)} Skill</label>`).join('');
    for (const item of catalog.skills) skills[item.id] = item.label;
    skillCatalog = catalog;
    applyRequiredSkills();
    retry.hidden = true; $('#sendBtn').disabled = !authenticated || submitting; notify('');
  } catch (error) {
    if (epoch !== authEpoch) return;
    skillCatalog = null;
    choices.textContent = '技能目录不可用';
    retry.hidden = false;
    notify('技能目录加载失败：' + error.message);
    return;
  } finally {
    if (epoch === authEpoch) { retry.disabled = false; choices.setAttribute('aria-busy', 'false'); }
  }
  renderLog(sessions[activeSessionId]);
}
function createSession(threadId, query, runId, select = true) {
  if (sessions[runId]) { if (select) switchSession(runId); return sessions[runId]; }
  const session = {id:threadId, runId, query, logs:[], seen:new Set(), stream:null, ws:null,
    status:'unknown', statusSeq:0, answer:null, error:null, recordingIncomplete:false};
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
  if (currentState) { session.statusSeq = event.event_seq; session.completion = data.completion; }
  if (currentState && event.event === 'run_queued') session.status = 'queued';
  if (currentState && event.event === 'run_started') session.status = 'running';
  if (currentState && event.event === 'task_result') {
    session.status = 'completed'; session.answer = data.result;
    session.recordingIncomplete = data.recording_status === 'incomplete';
  }
  if (currentState && ['error','run_cancelled','run_timed_out','run_interrupted'].includes(event.event)) {
    session.status = event.status; session.error = data.error;
    session.answer = null;
  }
  if (!activeStates.has(session.status) && session.status !== 'unknown') {
    if (session.stream) session.stream.close();
    refreshFiles();
  }
  renderTabs();
  if (activeSessionId === session.runId) renderLog(session);
}
function displayStatus(session) {
  if (session.status !== 'completed') return session.status;
  if (!session.completion) return 'coverage_unknown';
  return ({complete:'completed', unknown:'coverage_unknown'}[session.completion.status] || session.completion.status);
}
async function syncSession(session) {
  if (!authenticated || sessions[session.runId] !== session) return;
  try {
    const history = await request('/api/runs/' + encodeURIComponent(session.runId) + '/history');
    if (sessions[session.runId] !== session) return;
    if (history.run.thread_id !== session.id || history.run.run_id !== session.runId) throw new Error('运行归属不匹配');
    session.subtasks = history.subtasks || [];
    for (const e of history.events) applyEvent(session, e);
    // A live terminal event may arrive while this earlier HTTP snapshot is in flight.
    if (activeStates.has(session.status) || session.status === 'unknown' || !activeStates.has(history.run.status)) {
      session.status = history.run.status;
      session.completion = history.run.completion;
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
      }, checkAuthentication);
      // These events are already applied, so reconnect from the last contiguous event.
      for (const e of session.logs) session.stream.seen.add(session.runId + ':' + e.event_seq);
      while (session.seen.has(session.stream.afterSeq + 1)) session.stream.afterSeq++;
      session.stream.connect();
    }
  } catch (error) {
    session.syncError = error.status === 404 ? '服务端找不到该轮记录；可移除此视图。' : '读取记录失败：' + error.message;
  }
  if (!authenticated || sessions[session.runId] !== session) return;
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
  if (event.event === 'model_request') return ({research:'资料收集（模型）',coordination:'任务协调（模型）',
    summarization:'上下文摘要（模型）'}[d.phase] || '模型请求');
  if (event.event === 'skill_state') return `Skill ${skills[d.skill_id] || d.skill_id}：` +
    ({selected:'已选择', loaded:'正文已读取', resource_loaded:'参考资源已读取'}[event.status] || event.status);
  if (event.event === 'tool_retry') return `运行时重试 #${d.attempt}：等待 ${Number(d.delay_seconds || 0).toFixed(2)} 秒`;
  if (event.status === 'cancelled' && ['on_error','after_tool'].includes(event.event)) {
    const reason = d.error && !/^<object object at 0x[0-9a-f]+>$/i.test(d.error) ? d.error : '当前操作已取消';
    return '操作已取消：' + reason;
  }
  if (event.event === 'after_tool' && d.execution_status === 'completed' && d.result_status === 'rejected') return (d.tool_name || '工具') + ' · 已执行，结果未获准：' + (d.error || event.status);
  if (event.event === 'after_tool' && d.execution_status === 'unknown') return (d.tool_name || '工具') + ' · 执行结果未确定：' + (d.error || event.status);
  if (event.event === 'after_tool') return (d.tool_name || event.actor?.split(':').at(-1) || '工具') + (event.status === 'completed' ? ' · 已完成' : ' · 未完成：' + (d.error || event.status));
  if (event.event === 'on_error') return '动作失败：' + (d.error || '请查看详情');
  if (event.event === 'before_tool') return '执行前检查 · ' + (d.tool_name || event.actor?.split(':').at(-1) || '工具');
  if (event.event === 'before_delegate') return `${who}执行前检查`;
  if (['subagent_queued','subagent_running','subagent_completed','subagent_failed','subagent_cancelled',
    'subagent_interrupted','subagent_rejected','subagent_timed_out','after_subagent'].includes(event.event))
    return `${who}：${statusNames[event.status] || event.status}${d.error ? '；' + d.error : ''}`;
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
    ${session.completion?.error ? `<p>${escapeHtml(typeof session.completion.error === 'string' ? session.completion.error : session.completion.error.message)}</p>` : ''}
    ${session.syncError ? `<p>${escapeHtml(session.syncError)}</p>` : ''}
    ${session.recordingIncomplete ? '<p>部分执行记录未保存，详情见最终事件。</p>' : ''}
    <details><summary>运行详情</summary><pre>${escapeHtml(JSON.stringify({thread_id:session.id,run_id:session.runId,status:session.status},null,2))}</pre></details>`;
  answer.hidden = session.answer === null || session.answer === undefined;
  renderAnswer(answer, session.answer ?? '');
  const area = $('#logArea');
  const follow = area.scrollHeight - area.scrollTop - area.clientHeight < 80;
  const openDetails = new Set([...area.querySelectorAll('details[open]')].map(d => d.dataset.seq));
  area.innerHTML = renderCallGroups(session, openDetails) || '<div class="empty-state">等待执行记录…</div>';
  if (follow) area.scrollTop = area.scrollHeight;
}
function callGroups(session) {
  const agents = new Map(), aliases = new Map();
  for (const event of session.logs) {
    if (['subagent_queued','subagent_running','subagent_completed','subagent_failed','subagent_cancelled',
      'subagent_interrupted','subagent_rejected','subagent_timed_out'].includes(event.event)) {
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
    const agentName = group.events.find(e => e.data?.assistant_name)?.data.assistant_name || actors[group.target] || group.target || '助手';
    const terminal = group.events.filter(e=>['subagent_completed','subagent_failed','subagent_cancelled','subagent_interrupted','subagent_rejected','subagent_timed_out'].includes(e.event)).at(-1);
    const outcome = terminal ? ' · ' + (statusNames[terminal.status] || terminal.status) : '';
    const title = index === 0 && group.target === 'main' ? '主助手' : `${agentName} · ${group.label || '独立分工'} · ${group.id.slice(0,8)}${outcome}`;
    const calls = new Map();
    for (const e of group.events) {
      const key = group.requestIds?.has(e.call_id) ? group.id : e.call_id;
      if (!calls.has(key)) calls.set(key,[]);
      calls.get(key).push(e);
    }
    let toolNumber = 0;
    const rows = [...calls.values()].map((events,i) => {
      const last = events.at(-1);
      const failed = ['failed','rejected','cancelled','interrupted','timed_out'].includes(last.status);
      const name = events.find(e=>e.data?.tool_name)?.data.tool_name || events.find(e=>e.actor?.includes(':'))?.actor.split(':').at(-1);
      const toolNames = {internet_search:'网络搜索',read_web_page:'网页正文读取',read_file:'读取文件',task:'委派任务'};
      const caption = name ? `${toolNames[name] || name} #${++toolNumber} · ${last.event==='after_tool' && last.status==='completed'?'已完成':describeEvent(last)}` : describeEvent(last);
      const end = new Date(last.timestamp);
      const measured = [...events].reverse().find(e => Number.isFinite(e.data?.elapsed_ms) &&
        (e.event === 'model_request' || ['after_tool','after_subagent'].includes(e.event)));
      const elapsedMs = measured?.data.elapsed_ms;
      const elapsed = Number.isFinite(elapsedMs) ? ` · ${(elapsedMs/1000).toFixed(1)} 秒` : '';
      const requestMetrics = events.find(e => e.event === 'model_request')?.data;
      const requestSize = Number.isFinite(requestMetrics?.request_bytes)
        ? ` · 请求 ${(requestMetrics.request_bytes/1024).toFixed(1)} KiB` : '';
      const queueTime = Number.isFinite(requestMetrics?.queue_wait_ms)
        ? ` · 排队 ${(requestMetrics.queue_wait_ms/1000).toFixed(1)} 秒` : '';
      const usage = requestMetrics ? ` · 输入/输出 Token：${Number.isFinite(requestMetrics.input_units)
        ? requestMetrics.input_units : '未知'}/${Number.isFinite(requestMetrics.output_units)
        ? requestMetrics.output_units : '未知'}` : '';
      return `<div class="log-entry" data-call="${escapeHtml(last.call_id)}"><div class="log-dot ${failed?'e':'a'}"></div><div class="log-body">
        <span class="log-msg">${escapeHtml(caption + elapsed + requestSize + queueTime + usage)}</span>
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
async function sendTask() {
  const epoch = authEpoch;
  const input = $('#queryInput'), query = input.value.trim();
  if (!authenticated || !query || submitting) return;
  if (skillCatalog === null) { notify('请先成功加载技能目录再提交任务'); return; }
  submitting = true; $('#sendBtn').disabled = true; notify('正在提交…');
  try {
    applyRequiredSkills();
    const skillIds = Array.from($('#skillChoices').querySelectorAll('input[name="skill_id"]:checked'), input => input.value);
    if (skillIds.length > skillCatalog.max_selected || new Set(skillIds).size !== skillIds.length ||
        skillIds.some(id => !Object.hasOwn(skills, id))) throw new Error('技能选择不符合服务端目录');
    const mode = $('#modeSelect').value;
    const data = await request('/api/task', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({query, input_text:$('#inputText').value || null,
        network_policy:$('#forbidNetwork').checked ? 'forbidden' : 'allowed',
        skill_ids:skillIds, business_mode:mode, thread_id:$('#threadSelect').value || null})});
    const session = createSession(data.thread_id, query, data.run_id);
    session.status = 'queued'; input.value = ''; $('#inputText').value = ''; notify('任务已接收');
    await syncSession(session);
  } catch (error) {
    if (epoch !== authEpoch) return;
    if (error.status === 409) {
      const session = createSession(error.data.thread_id, '该会话正在执行的任务', error.data.run_id);
      notify('该会话忙碌，已显示正在执行的任务。请等待、取消，或选择新会话。');
      await syncSession(session);
    } else notify('提交失败：' + error.message);
  } finally { if (epoch === authEpoch) { submitting = false; $('#sendBtn').disabled = !authenticated || skillCatalog === null; } }
}
async function cancelRun() {
  const session = sessions[activeSessionId];
  if (!session || !activeStates.has(session.status)) return;
  $('#cancelBtn').disabled = true;
  try {
    const record = await request('/api/runs/' + encodeURIComponent(session.runId) + '/cancel', {method:'POST'});
    session.status = record.status; session.answer = record.answer; session.error = record.error; session.completion = record.completion;
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
    if (!authenticated) return;
    const data = await request('/api/runs/' + encodeURIComponent(session.runId) + '/files');
    if (version !== fileRequest) return;
    list.innerHTML = data.files.map(f => `<div class="file-item"><div class="file-info">
      <div class="file-name">${escapeHtml(f.name)}</div><div class="file-meta">${f.size} B</div></div>
      <a class="btn btn-primary" href="${escapeHtml(f.url)}" target="_blank" rel="noopener">下载</a></div>`).join('') || '<div class="empty-state">本轮暂无文件</div>';
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
  if (!authenticated) return;
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
$('#modeSelect').addEventListener('change', applyRequiredSkills);
$('#retrySkillsBtn').addEventListener('click', () => loadSkillCatalog().catch(error => notify('技能选项更新失败：' + error.message)));
$('#queryInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTask(); } });
initializeAuthentication().catch(error => { $('#authMessage').textContent = error.message; });

