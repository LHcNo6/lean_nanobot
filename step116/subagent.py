from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from step116.bus import MessageBus
from step116.bus.events import InboundMessage
from step116.config.schema import ToolsConfig
from step116.context import RequestContext, ToolContext
from step116.hook import AgentHook, AgentHookContext
from step116.loader import ToolLoader
from step116.provider import LLMProvider
from step116.runner import AgentRunSpec, AgentRunner
from step116.tool import ToolRegistry
from step116.tools.exec_session import ExecSessionManager
from step116.tools.cli_apps import build_cli_app_manager
from step116.tools.file_state import FileStateStore
from step116.skills.loader import SkillsLoader


# ---------------------------------------------------------------------------
# 配置扁平化适配（step116）
#
# 工具的 enabled()/create() 按"扁平视图"读取配置：根级 ``.web``、``.exec``、
# ``.tools`` 三段直接可访问（tests 中的 SimpleNamespace 即此形态）。而完整
# pydantic Config 只有根级 ``.tools``（web/exec 嵌套其内），若直接传入，
# web/exec 组工具会在 enabled() 阶段抛 AttributeError，被 ToolLoader 的
# 静默异常处理吞掉而悄悄缺席。此适配器把多种输入形态统一为扁平视图。
# ---------------------------------------------------------------------------


def _copy_section_with(section: Any, **updates: Any) -> Any:
    """浅拷贝一个配置 section 并覆盖指定字段。

    兼容 pydantic 模型（model_copy）与普通 namespace 对象两种形态，
    避免原地修改共享的配置对象。

    Args:
        section: 原 section（pydantic 模型或 SimpleNamespace 等）。
        **updates: 需要覆盖的字段名到新值的映射。

    Returns:
        覆盖后的新 section 实例（原对象不被修改）。
    """
    if hasattr(section, "model_copy"):
        return section.model_copy(update=updates)
    data: dict[str, Any] = dict(vars(section)) if hasattr(section, "__dict__") else {}
    data.update(updates)
    return SimpleNamespace(**data)


def _flatten_tools_config(config: Any) -> tuple[Any, bool]:
    """把多种配置输入形态统一为工具期望的扁平视图。

    支持三种输入（见 design.md §2.2）：

    1. 完整 pydantic ``Config``：有根级 ``.tools`` 且无根级 ``.web`` ——
       从 ``config.tools`` 提取 web/exec/tools 三段；
    2. 已扁平 duck-view（如测试用 SimpleNamespace）：同时有根级
       ``.web`` 与 ``.tools`` —— 原样采用；
    3. ``None``：从空 ``ToolsConfig()`` 构造默认视图。

    Args:
        config: 上述任一形态的配置对象。

    Returns:
        ``(flat_view, restrict_to_workspace)`` 二元组：前者是含
        ``web``/``exec``/``tools`` 属性的扁平视图；后者是解析后的
        权限意图布尔值（取自 ``tools.restrict_to_workspace``）。
    """
    if config is not None and hasattr(config, "web") and hasattr(config, "tools"):
        # 形态 2：已扁平视图。仍重建一层自有 SimpleNamespace（仅取
        # web/exec/tools 三段），使后续 restrict 覆写不会原地修改调用方对象。
        tools_section = getattr(config, "tools")
        restrict = bool(getattr(tools_section, "restrict_to_workspace", False))
        flat = SimpleNamespace(
            web=getattr(config, "web"),
            exec=getattr(config, "exec"),
            tools=tools_section,
        )
        return flat, restrict
    if config is not None and hasattr(config, "tools"):
        # 形态 1：完整 pydantic Config，扁平化提升 web/exec 到根级。
        tools_section = getattr(config, "tools")
        restrict = bool(getattr(tools_section, "restrict_to_workspace", False))
        flat = SimpleNamespace(
            web=getattr(tools_section, "web", None),
            exec=getattr(tools_section, "exec", None),
            tools=tools_section,
        )
        return flat, restrict
    # 形态 3：无配置，使用默认 ToolsConfig 的全默认值。
    default_tools = ToolsConfig()
    flat = SimpleNamespace(
        web=default_tools.web,
        exec=default_tools.exec,
        tools=default_tools,
    )
    return flat, bool(default_tools.restrict_to_workspace)


@dataclass
class SubagentStatus:
    task_id: str
    label: str
    task_description: str
    started_at: float = 0.0
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list[Any] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None


class _SubagentHook(AgentHook):
    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_calls)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


_SUBAGENT_SYSTEM_PROMPT = """You are a subagent spawned by the main agent.
Stay focused on the assigned task. Use tools to complete it.
Your final response will be reported back to the main agent."""


def _extract_disabled_skills(config: Any) -> set[str]:
    """从配置安全提取需禁用的技能名集合（step116）。

    注意 ``_flatten_tools_config`` 不携带 ``agents`` 段，故须从原始 ``config``
    实参经 duck-typed ``getattr`` 链提取，缺失时回退为空集合。
    """
    if config is None:
        return set()
    agents = getattr(config, "agents", None)
    defaults = getattr(agents, "defaults", None) if agents is not None else None
    disabled = getattr(defaults, "disabled_skills", None) if defaults is not None else None
    return set(disabled or [])


def _render_subagent_system_prompt(workspace: str, skills_summary: str) -> str:
    """渲染子代理 system prompt（step116）。

    Args:
        workspace: 本子代理生效的工作区根路径。
        skills_summary: ``SkillsLoader.build_skills_summary()`` 的 markdown 摘要；
            为空（无技能 / 全部禁用）时省略 ``# Skills`` 段。

    Returns:
        拼接后的 system prompt 文本（base + Workspace + 可选 Skills）。
    """
    parts = [
        _SUBAGENT_SYSTEM_PROMPT,
        f"# Workspace\n\nYou are operating within the workspace: {workspace}",
    ]
    if skills_summary and skills_summary.strip():
        parts.append(
            "# Skills\n\n"
            "The following skills extend your capabilities. To use a skill, "
            "read its SKILL.md file using the read_file tool. "
            "Unavailable skills need dependencies installed first.\n\n"
            + skills_summary
        )
    return "\n\n---\n\n".join(parts)


class SubagentManager:
    """子代理生命周期管理器。

    step116 起不再复用主 agent 的工具注册表，而是通过 ``_build_tools()``
    以 ``scope="subagent"`` 构建独立裁剪版注册表（对齐 nanobot
    ``SubagentManager._build_tools``），在结构上杜绝子代理递归 spawn
    与调用主 agent 专属工具（message/create_goal/self 等）。
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider | None = None,
        *,
        config: Any = None,
        workspace: str = "",
        restrict_to_workspace: bool | None = None,
        max_concurrent_subagents: int = 5,
        max_iterations: int = 10,
    ):
        """初始化子代理管理器。

        Args:
            bus: 消息总线，announce 结果经其回注主循环。
            provider: LLM provider；None 时 spawn 的任务静默结束（测试友好）。
            config: 配置对象，支持完整 Config / 已扁平 duck-view / None，
                内部经 ``_flatten_tools_config`` 统一为扁平视图供工具装配。
            workspace: 子代理工具的工作区根目录（str）。
            restrict_to_workspace: 权限意图；显式 True/False 优先，None 时
                回落 ``config.tools.restrict_to_workspace``。
            max_concurrent_subagents: 全局并发子代理上限（spawn 闸门）。
            max_iterations: 单个子代理的最大工具迭代次数。
        """
        self.bus = bus
        self._provider = provider
        flat_config, resolved_restrict = _flatten_tools_config(config)
        if restrict_to_workspace is not None:
            # 显式实参优先于配置声明，并同步覆写视图内 tools 段，
            # 使文件类工具的 allowed_dir 以装配意图为准。
            if resolved_restrict != restrict_to_workspace:
                flat_config.tools = _copy_section_with(
                    flat_config.tools, restrict_to_workspace=restrict_to_workspace
                )
            resolved_restrict = restrict_to_workspace
        self._config = flat_config
        self._workspace = workspace
        self._restrict_to_workspace = resolved_restrict
        self.max_concurrent_subagents = max_concurrent_subagents
        self.max_iterations = max_iterations
        self.runner = AgentRunner()
        # step116：manager 自己持有的长命令会话管理器，跨子代理共享
        # （对齐 nanobot：共享 ExecSessionManager + 独立 FileStates）。
        self._exec_session_manager = ExecSessionManager()
        # step116：CLI 应用白名单管理器（从 config.cli_apps.apps 注册，缺省空）
        self._cli_app_manager = build_cli_app_manager(getattr(config, "cli_apps", None))
        # step116：从原始 config 提取禁用技能，构造子代理技能加载器
        self._disabled_skills = _extract_disabled_skills(config)
        self._skills_loader = SkillsLoader(
            workspace=self._workspace, disabled_skills=self._disabled_skills
        )
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}

    def _build_subagent_system_prompt(self, workspace: str) -> str:
        """构建子代理 system prompt（step116）。

        聚合 base 角色设定 + 工作区信息 + 可用技能摘要，对齐主代理
        ``ContextBuilder.build_system_prompt`` 的 ``# Skills`` 措辞。

        Args:
            workspace: 本子代理生效的工作区根路径。

        Returns:
            完整的 system prompt 文本（含 ``# Workspace`` 段、可选的 ``# Skills`` 段）。
        """
        skills_summary = self._skills_loader.build_skills_summary()
        return _render_subagent_system_prompt(workspace, skills_summary)

    def _build_tools(self) -> ToolRegistry:
        """构建子代理专用的裁剪版工具注册表。

        通过 ``ToolLoader.load(scope="subagent")`` 只装载声明了 subagent
        scope 的工具（exec/文件/搜索/web/write_stdin/apply_patch/run_cli_app/
        list_exec_sessions 共 13 个）；
        spawn/message/create_goal 等核心专属工具因 scope 不含 "subagent"
        而天然不可见 —— 结构性防递归。

        双保险设计：
        - ToolContext 刻意不注入 bus/subagent_manager/sessions 等依赖，
          即使未来误将核心工具标上 subagent scope，其 create() 也会因
          缺依赖失败而被 loader 跳过；
        - file_state_store 每次构建全新实例，保证并发子代理的文件读写
          去重状态互不污染（对齐 nanobot 每次构建新 FileStates）。

        Returns:
            仅含 subagent-scope 工具的全新 ToolRegistry 实例。
        """
        registry = ToolRegistry()
        tool_ctx = ToolContext(
            config=self._config,
            workspace=str(Path(self._workspace).resolve()) if self._workspace else "",
            restrict_to_workspace=self._restrict_to_workspace,
            exec_session_manager=self._exec_session_manager,
            file_state_store=FileStateStore(),
            cli_app_manager=self._cli_app_manager,
        )
        ToolLoader().load(tool_ctx, registry, scope="subagent")
        return registry

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        *,
        origin: dict | None = None,
    ) -> str:
        running = self.get_running_count()
        if running >= self.max_concurrent_subagents:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{self.max_concurrent_subagents} running)."
            )
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=0.0,
        )
        self._task_statuses[task_id] = status

        # 合并 origin：以字典优先，回退到旧版形参，保证向后兼容
        # （既有 spawn(task=...) / spawn(task=..., session_key="s1") 调用不变）。
        merged = dict(origin or {})
        merged.setdefault("channel", origin_channel)
        merged.setdefault("chat_id", origin_chat_id)
        merged.setdefault("session_key", session_key)
        origin = merged
        session_key = origin.get("session_key")

        bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin, status))
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_):
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll report back when done."

    async def _run_subagent(self, task_id: str, task: str, label: str, origin: dict, status: SubagentStatus) -> None:
        """执行子代理任务体（后台协程）。

        step116：工具集改为每次 spawn 时经 ``_build_tools()`` 新建 ——
        子代理只能看到裁剪后的 subagent-scope 工具（对齐 nanobot
        ``_run_subagent`` 中 per-spawn 调用 ``_build_tools`` 的结构，
        为将来按 workspace_scope 差异化构建留出空间）。

        step116 新增：由 ``origin`` 重建 ``RequestContext`` 与 ``workspace_scope``
        并填入 ``AgentRunSpec``，使子代理在「父会话的上下文」中执行
        （``current_request_session_key`` / ``current_workspace_scope`` 可用），
        对齐 nanobot 的上下文绑定；绑定由 ``runner.run`` 统一完成，此处仅填字段。

        Args:
            task_id: 任务唯一标识（uuid 前 8 位）。
            task: 用户任务描述。
            label: 展示标签。
            origin: 路由 + 上下文信息 dict（channel/chat_id/session_key/
                message_id/runtime/workspace_scope）。
            status: 本任务的实时状态对象。
        """
        if self._provider is None:
            return
        # step116：从 origin 取出 workspace_scope，优先用以渲染 prompt 的工作区根；
        # 回落 self._workspace（测试/无 scope 时）。
        ws_scope = origin.get("workspace_scope")
        ws_path = str(ws_scope.project_path) if ws_scope else str(self._workspace)
        system_prompt = self._build_subagent_system_prompt(ws_path)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        # step116：从 origin 重建请求上下文与 workspace 范围，注入 AgentRunSpec；
        # runner.run 会据此 bind_request_context / bind_workspace_scope。
        req_ctx = RequestContext(
            channel=origin.get("channel") or "cli",
            chat_id=origin.get("chat_id") or "direct",
            session_key=origin.get("session_key"),
            message_id=origin.get("message_id"),
            runtime=origin.get("runtime"),
        )
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=self._build_tools(),
                provider=self._provider,
                max_iterations=self.max_iterations,
                hook=_SubagentHook(task_id, status),
                request_context=req_ctx,
                workspace_scope=ws_scope,
            ))
            status.phase = "done"
            status.stop_reason = result.stop_reason
            final = result.final_content or "Task completed."
            await self._announce(task_id, label, task, final, origin, "ok")
        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            await self._announce(task_id, label, task, f"Error: {e}", origin, "error")

    async def _announce(self, task_id: str, label: str, task: str, result: str, origin: dict, status: str) -> None:
        status_text = "completed" if status == "ok" else "failed"
        content = (
            f"[Subagent '{label}' {status_text}]\n\n"
            f"Task: {task}\n\nResult:\n{result}\n\n"
            f"Summarize this naturally for the user. Keep it brief."
        )
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata = {"injected_event": "subagent_result", "subagent_task_id": task_id}
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=override,
            content=content,
            session_key_override=override,
            metadata=metadata,
        )
        await self.bus.publish_inbound(msg)

    async def cancel_by_session(self, session_key: str) -> int:
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        tids = self._session_tasks.get(session_key, set())
        return sum(1 for tid in tids if tid in self._running_tasks and not self._running_tasks[tid].done())
