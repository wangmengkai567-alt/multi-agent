"""One collection boundary for every expert, independent of its source type."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage

from agent.runtime.context import get_run_context
from agent.runtime.errors import CollectionFinished, ExecutionTerminated
from agent.runtime.events import make_event

collection_scope = ContextVar("collection_scope", default=None)


@dataclass(frozen=True)
class CollectionScope:
    context: object
    task_id: str
    owner: str
    target: str
    pending: set = field(default_factory=set, compare=False, repr=False)

    @property
    def state(self):
        return self.context.collection_states[self.task_id]

    def remaining(self):
        remaining = self.context.remaining()
        if remaining is None:
            return None
        from agent.runtime.capacity import pending_seconds

        return remaining - pending_seconds(self.context) - self.context.policy.timeouts.shutdown_seconds

    def close(self, reason):
        context = self.context
        with context.budget_lock:
            if not self.state["closed_reason"]:
                self.state["closed_reason"] = reason
                from agent.runtime.checkpoints import save_job_checkpoint

                save_job_checkpoint(context)
                if context.event_sink is not None:
                    context.event_sink.submit(
                        make_event(
                            context.thread_id,
                            context.run_id,
                            "collection_stopped",
                            "专家资料采集已停止，已有依据交由报告步骤处理",
                            dict(task_id=self.task_id, **self.state),
                            actor=self.target,
                            status="partial",
                            call_id=self.owner,
                            parent_call_id=context.run_id,
                        )
                    )
            return CollectionFinished(self.task_id, self.state["closed_reason"])

    def finish(self, reason):
        raise self.close(reason)

    def check(self, kind=None, amount=1):
        context = self.context
        with context.budget_lock:
            context.check_active()
            if self.state["closed_reason"]:
                self.finish(self.state["closed_reason"])
            if not context.collecting:
                self.finish("run_finalizing")
            remaining = self.remaining()
            if remaining is not None and remaining <= 0:
                self.finish("time_limit")
            limits = context.policy.collection
            if kind == "sub_model" and self.state["model_calls"] + amount > limits.model_calls:
                self.finish("model_limit")
            if (
                kind == "tool"
                and self.state["tool_calls"] + amount > limits.tool_calls
                or kind == "sub_model"
                and self.state["tool_calls"] >= limits.tool_calls
            ):
                self.finish("tool_limit")

    async def settle(self):
        pending = [task for task in self.pending if task is not asyncio.current_task() and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, ExecutionTerminated):
                    self.context.terminate(outcome)

    def record(self, requested):
        for kind, counter in (("sub_model", "model_calls"), ("tool", "tool_calls")):
            self.state[counter] += requested.get(kind, 0)


def current_collection():
    scope = collection_scope.get()
    return scope if scope is not None and scope.context is get_run_context() else None


def open_collection():
    from agent.runtime.hooks import active_call
    from agent.runtime.policies import MissingConfiguration

    context, call = get_run_context(), active_call.get()
    if context is None or call is None or not call.search_task_key:
        raise MissingConfiguration("Expert collection requires an owned task context")
    with context.budget_lock:
        if not isinstance(context.collection_states, dict):
            raise MissingConfiguration("Invalid saved collection state")
        state = context.collection_states.setdefault(
            call.search_task_key,
            dict(target=call.target, model_calls=0, tool_calls=0, closed_reason=""),
        )
        if (
            not isinstance(state, dict)
            or set(state) != {"target", "model_calls", "tool_calls", "closed_reason"}
            or state["target"] != call.target
            or not isinstance(state["closed_reason"], str)
            or any(type(state[key]) is not int or state[key] < 0 for key in ("model_calls", "tool_calls"))
        ):
            raise MissingConfiguration("Saved collection identity or counters are invalid")
    return CollectionScope(context, call.search_task_key, call.call_id, call.target)


def collection_result(inputs, finished):
    return dict(
        inputs,
        messages=[
            AIMessage(
                content="资料采集阶段已停止；报告仅使用实际获得的依据，未完成部分保留为缺口。",
                additional_kwargs={"collection_stop_reason": finished.reason},
            )
        ],
    )


class CollectionExecutionMiddleware(AgentMiddleware):
    """Stop before sampling; tool and summarization admission share the same scope."""

    @staticmethod
    def prepare_request(request):
        scope = current_collection()
        if scope is None:
            from agent.runtime.policies import MissingConfiguration

            raise MissingConfiguration("Expert model requires an active collection scope")
        try:
            scope.check("sub_model")
        except CollectionFinished as finished:
            return ModelResponse(result=collection_result({}, finished)["messages"])
        return request

    def wrap_model_call(self, request, handler):
        prepared = self.prepare_request(request)
        return prepared if isinstance(prepared, ModelResponse) else handler(prepared)

    async def awrap_model_call(self, request, handler):
        prepared = self.prepare_request(request)
        return prepared if isinstance(prepared, ModelResponse) else await handler(prepared)
