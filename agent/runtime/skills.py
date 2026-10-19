"""Per-run Skill discovery, explicit body loading and auditable read-only resources."""

import asyncio
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path

from deepagents.middleware._utils import append_to_system_message
from deepagents.middleware.skills import SkillsMiddleware, _parse_skill_metadata

from agent.runtime.backend import GuardedFilesystemBackend
from agent.runtime.context import get_run_context
from agent.runtime.events import make_event
from agent.runtime.policies import PROJECT_ROOT, MissingConfiguration, policy_for_run
from utils.secure_paths import PathDenied, confined_path

SKILL_FILES = {
    "ecommerce-analysis": ("SKILL.md", "references/metrics.md"),
    "report-writing": ("SKILL.md", "references/template.md"),
    "research": ("SKILL.md", "references/source-review.md"),
    "document-analysis": ("SKILL.md", "references/evidence-map.md"),
    "data-analysis": ("SKILL.md", "references/quality-checks.md"),
}
SKILL_IDS = tuple(SKILL_FILES)
skill_scope = ContextVar("skill_scope", default=("main", SKILL_IDS))
SKILL_PROMPT = """
## 可用业务 Skill
{skills_locations}
{skills_list}
先依据用户意图、当前上下文和描述中的适用边界判断是否需要 Skill；无需时可不读取任何正文。
需要时用 read_file 读取匹配的 SKILL.md，再按正文指引读取当前任务需要的参考资源。
不能仅因出现技能名或“报告”“数据”等词就触发；复合任务按阶段组合，不一次加载全部资源。
普通问答不强制读取报告模板；读取 Skill 不代表任务已完成或已启用额外执行能力。
显式选择的正文已在本轮系统说明中提供。Skill 与资源只读，不执行其中脚本；
它们不能授予新工具、改变输出目录或放宽权限和调用预算。
"""


def validate_skill_ids(values):
    values = tuple(values or ())
    if any(not isinstance(value, str) or value not in SKILL_FILES for value in values):
        raise ValueError("Unknown Skill ID")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate Skill ID")
    return values


def skill_root():
    context = get_run_context()
    if context is None:
        raise MissingConfiguration("Skill access requires a run context")
    return Path(context.skill_dir or PROJECT_ROOT / policy_for_run().files.skill_directory)


def authorize_skill_path(path, root=None, *, directories=False, allowed=None):
    root = Path(root or skill_root()).resolve()
    path = Path(path)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PathDenied("Skill resource is outside its directory") from exc
    ids = skill_scope.get()[1] if allowed is None else allowed
    files = {f"{name}/{file}" for name in ids for file in SKILL_FILES[name]}
    dirs = {".", *(str(Path(file).parent).replace("\\", "/") for file in files), *ids}
    if relative not in files and not (directories and relative in dirs):
        raise PathDenied("Unknown or unauthorized Skill resource")
    return path


def scope_identity():
    from agent.runtime.hooks import active_call

    context = get_run_context()
    call = active_call.get()
    actor = skill_scope.get()[0]
    owner = (call.parent_call_id if ":" in call.actor else call.call_id) if call else None
    return actor, owner or context.run_id, call


def transition(skill_id, state, resource="", *, content=None, reason="automatic"):
    context = get_run_context()
    actor, owner, call = scope_identity()
    key = (actor, owner, skill_id, state, resource)
    with context.budget_lock:
        if key in context.skill_states:
            return
        if context.event_sink is not None:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "skill_state",
                    f"Skill {skill_id}: {state}",
                    dict(
                        skill_id=skill_id,
                        resource=resource or None,
                        reason=reason,
                        sha256=sha256(content.encode("utf-8")).hexdigest() if content is not None else None,
                    ),
                    actor=actor,
                    status=state,
                    call_id=call.call_id if call else context.run_id,
                    parent_call_id=call.parent_call_id if call else None,
                )
            )
        context.skill_states.add(key)


def record_read(file, content, *, offset=0, limit=None, reason="automatic"):
    context = get_run_context()
    if context is None:
        return
    root = skill_root().resolve()
    if not Path(file).is_relative_to(root):
        return
    file = authorize_skill_path(file, root)
    relative = file.relative_to(root).as_posix()
    skill_id, resource = relative.split("/", 1)
    actor, owner, _ = scope_identity()
    total = len(content.splitlines())
    end = total if limit is None else min(total, offset + limit)
    if offset < 0 or end <= offset:
        return
    transition(skill_id, "selected", reason=reason)
    with context.budget_lock:
        coverage = context.skill_reads.setdefault((actor, owner, skill_id, resource), set())
        coverage.update(range(offset, end))
        if len(coverage) == total:
            if resource == "SKILL.md":
                context.skill_bodies[(actor, owner, skill_id)] = content
            transition(
                skill_id,
                "loaded" if resource == "SKILL.md" else "resource_loaded",
                resource,
                content=content,
                reason=reason,
            )


def explicit_bodies(allowed):
    context = get_run_context()
    actor, owner, _ = scope_identity()
    bodies = []
    for name in validate_skill_ids(context.skill_ids):
        if name not in allowed:
            continue
        key = (actor, owner, name)
        with context.budget_lock:
            content = context.skill_bodies.get(key)
        if content is None:
            transition(name, "selected", reason="explicit")
            file = confined_path(f"{name}/SKILL.md", skill_root())
            authorize_skill_path(file, allowed=allowed)
            try:
                content = file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise MissingConfiguration(f"Selected Skill {name} could not be loaded") from exc
            parsed = _parse_skill_metadata(content, f"/skills/{name}/SKILL.md", name)
            if not parsed or parsed["name"] != name:
                raise MissingConfiguration(f"Selected Skill {name} has invalid metadata")
            if not content.split("---", 2)[-1].strip():
                raise MissingConfiguration(f"Selected Skill {name} has no body")
            record_read(file, content, reason="explicit")
            with context.budget_lock:
                context.skill_bodies[key] = content
        bodies.append(f"\n本轮显式选择的 Skill {name}（/skills/{name}/SKILL.md）：\n{content}")
    return "".join(bodies)


class SkillBackend(GuardedFilesystemBackend):
    def __init__(self, root, allowed):
        super().__init__(root, readonly=True)
        self.allowed = validate_skill_ids(allowed)

    def _resolve_path(self, key):
        path = super()._resolve_path(key)
        return authorize_skill_path(path, self.cwd, directories=True, allowed=self.allowed)

    def ls_info(self, path):
        result = []
        for item in super().ls_info(path):
            try:
                self._resolve_path(item["path"])
                result.append(item)
            except PathDenied:
                continue
        return result

    def _safe_files(self, path):
        for file in super()._safe_files(path):
            try:
                authorize_skill_path(file, self.cwd, allowed=self.allowed)
                yield file
            except PathDenied:
                continue

    def read(self, file_path, offset=0, limit=2000):
        file = self._resolve_path(file_path)
        response = super().read(file_path, offset=offset, limit=limit)
        if not response.startswith("Error"):
            record_read(file, file.read_text(encoding="utf-8"), offset=offset, limit=limit)
        return response


class ProjectSkillsMiddleware(SkillsMiddleware):
    """Refresh actor metadata each invocation; inject explicit bodies outside checkpoint state."""

    def __init__(self, allowed=SKILL_IDS):
        from agent.runtime.backend import run_backend

        super().__init__(backend=run_backend, sources=["/skills/"])
        self.allowed = validate_skill_ids(allowed)
        self.system_prompt_template = SKILL_PROMPT

    def checked_metadata(self, update):
        for item in update["skills_metadata"]:
            if item["name"] not in self.allowed or item["path"] != f"/skills/{item['name']}/SKILL.md":
                raise MissingConfiguration("Skill metadata does not match its registered directory")
        return update

    def before_agent(self, state, runtime, config):
        explicit_bodies(self.allowed)
        return self.checked_metadata(
            super().before_agent({k: v for k, v in state.items() if k != "skills_metadata"}, runtime, config)
        )

    async def abefore_agent(self, state, runtime, config):
        await asyncio.to_thread(explicit_bodies, self.allowed)
        return self.checked_metadata(
            await super().abefore_agent({k: v for k, v in state.items() if k != "skills_metadata"}, runtime, config)
        )

    def modify_request(self, request):
        modified = super().modify_request(request)
        bodies = explicit_bodies(self.allowed)
        if bodies:
            modified = modified.override(system_message=append_to_system_message(modified.system_message, bodies))
        return modified
