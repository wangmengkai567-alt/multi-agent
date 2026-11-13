"""Control signals have separate boundaries from model-correctable tool errors."""


class ExecutionTerminated(RuntimeError):
    """The run must stop; do not turn this exception into model feedback."""


class CollectionEnded(RuntimeError):
    """The acquisition phase ended; the owning coordinator must enter final reporting."""


class StageEnded(RuntimeError):
    """Only this expert stage ended; the coordinator may still execute downstream work."""


class StageCleanupFailed(ExecutionTerminated):
    """An owned stage did not release or persist within its cleanup boundary."""


class ContextCapacityExceeded(ExecutionTerminated):
    """Reject oversized context before transport; never retry with silently shortened instructions."""

    def __init__(self, message, *, used, limit):
        self.used, self.limit = used, limit
        super().__init__(f"{message}（占用 {used}，上限 {limit}）；请缩小本轮材料或拆分 Skill 参考资源")


class DependencyUnavailable(RuntimeError):
    """End the affected expert branch; this dependency cannot be fixed by its model."""
