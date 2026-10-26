"""Control signals have separate boundaries from model-correctable tool errors."""


class ExecutionTerminated(RuntimeError):
    """The run must stop; do not turn this exception into model feedback."""


class DependencyUnavailable(RuntimeError):
    """End the affected expert branch; this dependency cannot be fixed by its model."""


class CollectionFinished(Exception):
    """End one expert's collection and continue with its tool-free report."""

    def __init__(self, task_id, reason):
        self.task_id, self.reason = task_id, reason
        super().__init__(f"Collection finished: {reason}")
