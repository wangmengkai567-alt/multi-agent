/* 持久事件订阅：按运行 ID 和事件序号去重，断线后携带游标补齐历史。 */
class RunEventStream {
  constructor(threadId, runId, onEvent, onState, checkAuthentication) {
    this.threadId = threadId;
    this.runId = runId;
    this.onEvent = onEvent;
    this.onState = onState;
    this.checkAuthentication = checkAuthentication;
    this.afterSeq = 0;
    this.seen = new Set();
    this.closed = false;
    this.timer = null;
    this.socket = null;
    this.attempt = 0;
  }
  connect() {
    if (this.closed) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({run_id: this.runId, after_seq: String(this.afterSeq)});
    const socket = new WebSocket(proto + '//' + location.host + '/ws/' + encodeURIComponent(this.threadId) + '?' + params);
    this.socket = socket;
    this.onState(socket);
    socket.onopen = () => { this.attempt = 0; this.onState(socket); };
    socket.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch (_) { return; }
      if (event.type !== 'monitor_event' || event.run_id !== this.runId || event.thread_id !== this.threadId) return;
      if (!Number.isSafeInteger(event.event_seq) || event.event_seq < 1) return;
      const id = event.run_id + ':' + event.event_seq;
      if (this.seen.has(id)) return;
      // 先应用事件，再更新游标；若页面处理失败，重连后仍能回放该事件。
      this.onEvent(event);
      this.seen.add(id);
      // 仅跨过连续收到的序号，乱序收到较大序号时保留缺口供下一次重连补齐。
      while (this.seen.has(this.runId + ':' + (this.afterSeq + 1))) this.afterSeq++;
    };
    socket.onerror = () => socket.close();
    socket.onclose = event => {
      this.onState(null);
      if (event.code === 4403) { this.close(); return; }
      if (!this.closed) {
        // 断线重连逐步延长间隔，上限 10 秒；重连前先确认登录仍有效。
        const delay = Math.min(10000, 500 * 2 ** Math.min(this.attempt++, 5));
        this.timer = setTimeout(async () => {
          this.timer = null;
          try {
            if (!await this.checkAuthentication() || this.closed) return;
            this.connect();
          } catch (_) {
            if (!this.closed) socket.onclose({code:1006});
          }
        }, delay);
      }
    };
  }
  close() {
    this.closed = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (this.socket) this.socket.close();
  }
}
