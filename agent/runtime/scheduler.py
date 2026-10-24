"""One-process FIFO admission, child concurrency and per-account request pacing."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, copy_context
from hashlib import sha256
from threading import RLock
from time import monotonic, sleep
from uuid import uuid4

_run_owner = ContextVar("scheduler_run_owner", default=None)
child_owner = ContextVar("expert_execution_branch", default=None)


def detached_context():
    context = copy_context()
    context.run(_run_owner.set, None)
    context.run(child_owner.set, None)
    from agent.runtime.context import set_run_context

    context.run(set_run_context, None)
    return context


class QueueFull(RuntimeError):
    pass


def account_group(service, endpoint, credential):
    return service + ":" + sha256((endpoint.rstrip("/") + "\0" + credential).encode()).hexdigest()[:20]


class Gate:
    def __init__(self, limits):
        self.limits = limits
        self.lock = RLock()
        self.waiters = deque()
        self.active = 0
        self.next_start = 0.0

    def enqueue(self):
        with self.lock:
            # An available slot does not consume queue capacity.
            if self.active + len(self.waiters) >= self.limits.concurrency + self.limits.queue_capacity:
                raise QueueFull("Scheduling queue is full")
            ticket = str(uuid4())
            self.waiters.append(ticket)
            return ticket

    def ready(self, ticket):
        with self.lock:
            if not self.waiters or self.waiters[0] != ticket or self.active >= self.limits.concurrency:
                return False
            current = monotonic()
            if current < self.next_start:
                return False
            self.waiters.popleft()
            self.active += 1
            if self.limits.requests_per_second:
                self.next_start = current + 1.0 / self.limits.requests_per_second
            return True

    def cancel(self, ticket):
        with self.lock:
            if ticket in self.waiters:
                self.waiters.remove(ticket)

    def release(self):
        with self.lock:
            if self.active <= 0:
                raise RuntimeError("Scheduling lease released twice")
            self.active -= 1


class Lease:
    def __init__(self, gate):
        self.gate = gate

    def release(self):
        if self.gate is not None:
            self.gate.release()
            self.gate = None


class ServiceScheduler:
    def __init__(self, limits):
        self.limits = limits
        self.lock = RLock()
        self.gates = {"runs": Gate(limits.runs), "children": Gate(limits.children)}
        self.run_children = {}

    def gate(self, kind, group=""):
        key = kind + (":" + group if group else "")
        with self.lock:
            if key not in self.gates:
                self.gates[key] = Gate(getattr(self.limits, kind))
            return self.gates[key]

    def expert_gate(self, resources):
        from agent.runtime.policies import PoolLimits

        key = "expert:" + resources.group
        limits = PoolLimits(concurrency=resources.concurrency, queue_capacity=resources.queue_capacity)
        with self.lock:
            if key in self.gates and self.gates[key].limits != limits:
                previous = self.gates[key]
                with previous.lock:
                    if previous.active or previous.waiters:
                        raise ValueError("Expert resource group limits changed during execution")
                    self.gates[key] = Gate(limits)
            if key not in self.gates:
                self.gates[key] = Gate(limits)
            return self.gates[key]

    def child_gate(self, context):
        from agent.runtime.policies import PoolLimits

        with self.lock:
            if context.run_id not in self.run_children:
                self.run_children[context.run_id] = Gate(
                    PoolLimits(
                        concurrency=context.policy.search.max_concurrent_tasks,
                        queue_capacity=context.policy.search.max_tasks_per_run,
                    )
                )
            return self.run_children[context.run_id]

    def forget_run(self, run_id):
        with self.lock:
            gate = self.run_children.get(run_id)
            if gate is not None:
                with gate.lock:
                    if not gate.active and not gate.waiters:
                        del self.run_children[run_id]

    @staticmethod
    def event(context, kind, label, started):
        from agent.runtime.events import make_event
        from agent.runtime.hooks import active_call

        call = active_call.get()
        if context.event_sink:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "scheduler_" + kind,
                    "排队等待" if kind == "queued" else "取得执行槽",
                    {"resource": label, "queue_wait_ms": round((monotonic() - started) * 1000)},
                    status="queued" if kind == "queued" else "running",
                    actor=call.target if call and call.target else "main",
                    call_id=call.call_id if call else context.run_id,
                    parent_call_id=call.parent_call_id if call else None,
                )
            )

    def acquire(self, gate, context, label):
        context.check_active()
        ticket, started = gate.enqueue(), monotonic()
        try:
            if not gate.ready(ticket):
                self.event(context, "queued", label, started)
                while True:
                    context.check_active()
                    if gate.ready(ticket):
                        break
                    # No network, model budget or execution slot is held by a waiter.
                    sleep(0.02)
            context.check_active()
            self.event(context, "started", label, started)
            return Lease(gate)
        except BaseException:
            # ready() has consumed the ticket iff it is absent; release only our lease.
            with gate.lock:
                if ticket not in gate.waiters:
                    gate.release()
                else:
                    gate.cancel(ticket)
            raise

    async def aacquire(self, gate, context, label, ticket=None):
        if ticket is None:
            context.check_active()
            ticket = gate.enqueue()
        started = monotonic()
        acquired = False
        try:
            context.check_active()
            if not gate.ready(ticket):
                self.event(context, "queued", label, started)
                while True:
                    context.check_active()
                    if gate.ready(ticket):
                        break
                    await asyncio.sleep(0.02)
            acquired = True
            context.check_active()
            self.event(context, "started", label, started)
            return Lease(gate)
        except BaseException:
            if acquired:
                gate.release()
            else:
                gate.cancel(ticket)
            raise

    @asynccontextmanager
    async def run_slot(self, context):
        if _run_owner.get() is self:
            # A durable job owns the same service slot throughout its handler.
            yield
            return
        lease = await self.aacquire(self.gate("runs"), context, "runs", context.scheduler_ticket)
        token = _run_owner.set(self)
        try:
            yield
        finally:
            _run_owner.reset(token)
            lease.release()
            self.forget_run(context.run_id)

    @contextmanager
    def provider(self, context, kind, group):
        lease = self.acquire(self.gate(kind, group), context, kind)
        try:
            yield
        finally:
            lease.release()

    @asynccontextmanager
    async def aprovider(self, context, kind, group):
        lease = await self.aacquire(self.gate(kind, group), context, kind)
        try:
            yield
        finally:
            lease.release()

    def snapshot(self):
        with self.lock:
            rows = {}
            for name, gate in self.gates.items():
                with gate.lock:
                    # Public metrics expose resource types, not account identifiers.
                    kind = name.split(":", 1)[0]
                    row = rows.setdefault(kind, {"active": 0, "queued": 0})
                    row["active"] += gate.active
                    row["queued"] += len(gate.waiters)
            return rows
