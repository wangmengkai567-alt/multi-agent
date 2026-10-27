/* Offline browser lifecycle tests using the production RunEventStream script. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sockets = [], timers = new Map();
let nextTimer = 0;
class Socket {
  constructor(url) { this.url = url; sockets.push(this); }
  close(code=1006) { if (this.onclose) this.onclose({code}); }
  emit(event) { this.onmessage({data: JSON.stringify(event)}); }
}
const sandbox = {
  WebSocket: Socket, URLSearchParams,
  location: {protocol: 'http:', host: 'localhost:8000'},
  setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../api/static/run-events.js'), 'utf8') + ';this.Stream = RunEventStream;', sandbox);
async function main() {
const received = [], states = [];
const stream = new sandbox.Stream('thread', 'run', e => received.push(e), s => states.push(s), async()=>true);
stream.connect();
const first = sockets[0];
assert.match(first.url, /run_id=run&after_seq=0/);
const event = n => ({type: 'monitor_event', thread_id: 'thread', run_id: 'run', event_seq: n, event: 'progress'});
first.emit(event(1)); first.emit(event(3)); first.emit(event(1));
assert.equal(stream.afterSeq, 1); // A gap must not advance the replay cursor.
first.emit({...event(2), run_id: 'other'});
first.emit({...event(2), thread_id: 'other'});
assert.equal(received.length, 2);
first.close();
assert.equal(timers.size, 1);
assert.equal(states.at(-1), null);
const reconnect = [...timers.values()][0]; timers.clear(); await reconnect();
const second = sockets[1];
assert.match(second.url, /after_seq=1/);
second.emit(event(2)); second.emit(event(3));
assert.equal(stream.afterSeq, 3);
assert.deepEqual(received.map(e => e.event_seq), [1, 3, 2]);
second.emit({...event(4), event: 'run_timed_out', status: 'timed_out'});
assert.equal(received.at(-1).status, 'timed_out');
second.close();
assert.equal(timers.size, 1);
stream.close();
assert.equal(timers.size, 0);
assert.equal(sockets.length, 2);
console.log('PASS: cursor gaps, reconnect cursor, duplicate and cross-run filtering, terminal events, explicit close');

}
main().catch(error=>{console.error(error);process.exitCode=1;});
