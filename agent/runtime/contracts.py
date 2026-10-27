"""Program-owned input snapshots and permissions, with no model routing prerequisite."""

from dataclasses import dataclass
from hashlib import sha256

from langchain.agents.middleware import AgentMiddleware

from agent.runtime.context import get_run_context
from utils.secure_paths import confined_path


@dataclass(frozen=True)
class BoundMaterial:
    kind: str
    reference: str
    text: str | None
    digest: str


@dataclass(frozen=True)
class TaskContract:
    user_query: str
    input_text: str | None
    network_policy: str
    required_sources: tuple[str, ...]
    required_uploads: tuple[str, ...]
    business_mode: str
    materials: tuple[BoundMaterial, ...]

    @property
    def instruction_text(self):
        return self.user_query

    @property
    def network_allowed(self):
        return self.network_policy == "allowed"


def bind_contract(context):
    context.check_active()
    if context.task_contract is not None:
        return assert_contract(context)
    directory = confined_path("uploads", context.work_dir)
    materials = []
    if directory.is_dir():
        for file in sorted(directory.iterdir()):
            file = confined_path(str(file), directory)
            if file.is_file():
                if file.stat().st_size > context.policy.files.max_upload_bytes:
                    raise ValueError("Original document exceeds the configured upload size limit")
                materials.append(
                    BoundMaterial("upload", "uploads/" + file.name, None, sha256(file.read_bytes()).hexdigest())
                )
    if context.input_text is not None:
        materials.append(
            BoundMaterial("request", "input_text", context.input_text, sha256(context.input_text.encode()).hexdigest())
        )
    context.task_contract = TaskContract(
        context.user_query,
        context.input_text,
        context.network_policy,
        context.required_sources,
        context.required_uploads,
        context.business_mode,
        tuple(materials),
    )
    return context.task_contract


def assert_contract(context):
    contract = context.task_contract
    if contract is None:
        raise ValueError("Task inputs must be bound before execution")
    if (
        contract.user_query,
        contract.input_text,
        contract.network_policy,
        contract.required_sources,
        contract.required_uploads,
        contract.business_mode,
    ) != (
        context.user_query,
        context.input_text,
        context.network_policy,
        context.required_sources,
        context.required_uploads,
        context.business_mode,
    ):
        raise ValueError("Locked task inputs or permissions changed")
    for material in contract.materials:
        if material.kind == "upload":
            path = confined_path(material.reference, context.work_dir)
            if (
                not path.is_file()
                or path.stat().st_size > context.policy.files.max_upload_bytes
                or sha256(path.read_bytes()).hexdigest() != material.digest
            ):
                raise ValueError("Bound upload changed")
    return contract


class TaskContractMiddleware(AgentMiddleware):
    def before_agent(self, state, runtime):
        bind_contract(get_run_context())

    async def abefore_agent(self, state, runtime):
        bind_contract(get_run_context())
