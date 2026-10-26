"""Control signals have separate boundaries from model-correctable tool errors."""


class ExecutionTerminated(RuntimeError):
    """The run must stop; do not turn this exception into model feedback."""


class ContextCapacityExceeded(ExecutionTerminated):
    """Reject oversized context before transport; never retry with silently shortened instructions."""

    def __init__(self, message, *, used, limit):
        self.used, self.limit = used, limit
        super().__init__(f"{message}（占用 {used}，上限 {limit}）；请缩小本轮材料或拆分 Skill 参考资源")


class DependencyUnavailable(RuntimeError):
    """End the affected expert branch; this dependency cannot be fixed by its model."""


class CollectionFinished(Exception):
    """End one expert's collection and continue with its tool-free report."""

    def __init__(self, task_id, reason):
        self.task_id, self.reason = task_id, reason
        super().__init__(f"Collection finished: {reason}")
