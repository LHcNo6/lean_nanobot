from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from step124.bus import MessageBus
from step124.bus.events import InboundMessage
from step124.config.schema import ToolsConfig
from step124.context import RequestContext, ToolContext
from step124.hook import AgentHook, AgentHookContext
from step124.loader import ToolLoader
from step124.provider import LLMProvider
from step124.governance import ContextGovernanceConfig
from step124.llm import GenerationSettings, LLMRuntime
from step124.runner import AgentRunSpec, AgentRunner
from step124.security.workspace_access import workspace_sandbox_status
from step124.tool import ToolRegistry
from step124.tools.exec_session import ExecSessionManager
from step124.tools.cli_apps import build_cli_app_manager
from step124.tools.file_state import FileStateStore
from step124.skills.loader import SkillsLoader


# ---------------------------------------------------------------------------
# 子代理 announce 模板渲染（step124，对齐 nanobot agent/subagent_announce.md）
# ---------------------------------------------------------------------------

_ANNOUNCE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "agent" / "subagent_announce.md"
_ANNOUNCE_TEMPLATE_FALLBACK = (
    "[Subagent '{{ label }}' {{ status_text }}]\n\n"
    "Task: {{ task }}\n\n"
    "Result:\n{{ result }}\n\n"
    "Summarize this naturally for the user. Keep it brief (1-2 sentences). "
    'Do not mention technical details like "subagent" or task IDs.'
)
_ANNOUNCE_TEMPLATE_CACHE: str | None = None
_ANNOUNCE_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _load_announce_template() -> str:
    """加载子代理 announce 模板（step124）。

    优先读取 ``templates/agent/subagent_announce.md``；读取失败（如打包缺失）
    回退内置常量，保证子代理 announce 始终可渲染。结果缓存在模块级，仅加载一次。
    """
    global _ANNOUNCE_TEMPLATE_CACHE
    if _ANNOUNCE_TEMPLATE_CACHE is not None:
        return _ANNOUNCE_TEMPLATE_CACHE
    try:
        text = _ANNOUNCE_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        text = _ANNOUNCE_TEMPLATE_FALLBACK
    _ANNOUNCE_TEMPLATE_CACHE = text
    return text


def _render_subagent_announce(label: str, status_text: str, task: str, result: str) -> str:
    """渲染子代理 announce 内容（step124，对齐 nanobot 模板）。

    Args:
        label: 子代理展示标签。
        status_text: 状态文案（"completed successfully" / "failed"）。
        task: 原始任务描述。
        result: 子代理最终/错误内容。

    Returns:
        渲染后的 announce 文本（头部 + Task + Result + Summarize 指令）。
    """
    template = _load_announce_template()
    ctx = {"label": label, "status_text": status_text, "task": task, "result": result}

    def _sub(m: "re.Match[str]") -> str:
        return str(ctx.get(m.group(1), ""))

    return _ANNOUNCE_VAR_RE.sub(_sub, template)


# ---------------------------------------------------------------------------
# 配置扁平化适配（step124）
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
    """从配置安全提取需禁用的技能名集合（step124）。

    注意 ``_flatten_tools_config`` 不携带 ``agents`` 段，故须从原始 ``config``
    实参经 duck-typed ``getattr`` 链提取，缺失时回退为空集合。
    """
    if config is None:
        return set()
    agents = getattr(config, "agents", None)
    defaults = getattr(agents, "defaults", None) if agents is not None else None
    disabled = getattr(defaults, "disabled_skills", None) if defaults is not None else None
    return set(disabled or [])


def _extract_max_tool_result_chars(config: Any) -> int:
    """从配置安全提取子代理工具结果截断字符数（step124）。

    对齐 nanobot 子代理 ``AgentRunSpec.max_tool_result_chars`` 传播。取自
    ``agents.defaults.max_tool_result_chars``（扁平视图不携带该段），缺失时
    回退默认 ``16_000``（与 ``ContextGovernanceConfig`` 默认一致）。允许值为
    ``0``（不截断），故以 ``None`` 判定缺省而非真值。
    """
    if config is None:
        return 16_000
    agents = getattr(config, "agents", None)
    defaults = getattr(agents, "defaults", None) if agents is not None else None
    value = getattr(defaults, "max_tool_result_chars", None) if defaults is not None else None
    if value is None:
        return 16_000
    return int(value)


def _extract_fail_on_tool_error(config: Any) -> bool:
    """从配置安全提取子代理是否将工具错误升级为失败（step124）。

    取自 ``agents.defaults.fail_on_tool_error``，缺失时回退默认 ``True``。
    """
    if config is None:
        return True
    agents = getattr(config, "agents", None)
    defaults = getattr(agents, "defaults", None) if agents is not None else None
    value = getattr(defaults, "fail_on_tool_error", None) if defaults is not None else None
    return bool(value) if value is not None else True


def _render_subagent_system_prompt(workspace: str, skills_summary: str) -> str:
    """渲染子代理 system prompt（step124）。

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

    step124 起不再复用主 agent 的工具注册表，而是通过 ``_build_tools()``
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
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
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
            llm_wall_timeout_for_session: 父会话墙钟超时解析回调
                ``Callable[[session_key], float | None]``（对齐 nanobot）。
                ``0.0`` 表示禁用子代理超时；``None`` 由 runner 用 env 默认（300s）。
                缺省 ``None`` 时子代理回退为 ``None``（与 step116 行为一致）。
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
        # step124：manager 自己持有的长命令会话管理器，跨子代理共享
        # （对齐 nanobot：共享 ExecSessionManager + 独立 FileStates）。
        self._exec_session_manager = ExecSessionManager()
        # step124：CLI 应用白名单管理器（从 config.cli_apps.apps 注册，缺省空）
        self._cli_app_manager = build_cli_app_manager(getattr(config, "cli_apps", None))
        # step124：从原始 config 提取禁用技能，构造子代理技能加载器
        self._disabled_skills = _extract_disabled_skills(config)
        # step124：从原始 config 提取子代理运行配置（对齐 nanobot 子代理运行配置传播）
        self._max_tool_result_chars = _extract_max_tool_result_chars(config)
        self._fail_on_tool_error = _extract_fail_on_tool_error(config)
        self._skills_loader = SkillsLoader(
            workspace=self._workspace, disabled_skills=self._disabled_skills
        )
        # step124：父会话墙钟超时解析回调（对齐 nanobot llm_wall_timeout_for_session）；
        # 缺省 None → 子代理 llm_timeout_s 回退为 None（env 默认 300s）。
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}

    def _build_subagent_system_prompt(self, workspace: str) -> str:
        """构建子代理 system prompt（step124）。

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
        root = str(Path(self._workspace).resolve()) if self._workspace else ""
        tool_ctx = ToolContext(
            config=self._config,
            workspace=root,
            restrict_to_workspace=self._restrict_to_workspace,
            exec_session_manager=self._exec_session_manager,
            file_state_store=FileStateStore(),
            cli_app_manager=self._cli_app_manager,
            # step124（G9）：对齐 nanobot，子代理 ToolContext 携带宿主 workspace 限制状态
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=self._restrict_to_workspace,
                workspace=root,
            ),
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
        temperature: float | None = None,
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
        # step124（G7）：temperature 覆写 —— 将覆写后的 runtime 写回 origin，
        # 沿 step122（G5）既有「runtime -> temperature 衍生」通道生效。
        if temperature is not None:
            rt = merged.get("runtime")
            if rt is None:
                rt = LLMRuntime(
                    provider=self._provider,
                    model="",
                    generation=GenerationSettings(),
                    context_window_tokens=8192,
                )
            merged["runtime"] = rt.with_generation_overrides(temperature=temperature)
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

        step121：工具集改为每次 spawn 时经 ``_build_tools()`` 新建 ——
        子代理只能看到裁剪后的 subagent-scope 工具（对齐 nanobot
        ``_run_subagent`` 中 per-spawn 调用 ``_build_tools`` 的结构，
        为将来按 workspace_scope 差异化构建留出空间）。

        step121 新增：由 ``origin`` 重建 ``RequestContext`` 与 ``workspace_scope``
        并填入 ``AgentRunSpec``，使子代理在「父会话的上下文」中执行
        （``current_request_session_key`` / ``current_workspace_scope`` 可用），
        对齐 nanobot 的上下文绑定；绑定由 ``runner.run`` 统一完成，此处仅填字段。

        step124（G5，runtime 逐父同步）：从 ``origin["runtime"]`` 衍生
        ``model`` / ``temperature`` / ``max_tokens`` 注入 ``AgentRunSpec``，
        使子代理继承父会话的模型与生成参数（provider 沿用 ``self._provider``，
        生产环境与 ``runtime.provider`` 为同一对象，终态行为等价）。

        Args:
            task_id: 任务唯一标识（uuid 前 8 位）。
            task: 用户任务描述。
            label: 展示标签。
            origin: 路由 + 上下文信息 dict（channel/chat_id/session_key/
                message_id/runtime/workspace_scope）。
            status: 本任务的实时状态对象。
        """
        # step122（G5）：从 origin 取出父会话 runtime，衍生子代理运行参数；
        # 生产环境 self._provider 与 runtime.provider 为同一对象，故 provider 沿用
        # self._provider 即可；仅当其为 None 时回退 runtime.provider。
        runtime = origin.get("runtime") if origin else None
        provider = self._provider or (getattr(runtime, "provider", None) if runtime else None)
        if provider is None:
            return
        # step121：从 origin 取出 workspace_scope，优先用以渲染 prompt 的工作区根；
        # 回落 self._workspace（测试/无 scope 时）。
        ws_scope = origin.get("workspace_scope")
        ws_path = str(ws_scope.project_path) if ws_scope else str(self._workspace)
        system_prompt = self._build_subagent_system_prompt(ws_path)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        # step121：从 origin 重建请求上下文与 workspace 范围，注入 AgentRunSpec；
        # runner.run 会据此 bind_request_context / bind_workspace_scope。
        req_ctx = RequestContext(
            channel=origin.get("channel") or "cli",
            chat_id=origin.get("chat_id") or "direct",
            session_key=origin.get("session_key"),
            message_id=origin.get("message_id"),
            runtime=origin.get("runtime"),
        )
        # step121：以父会话 session_key 解析墙钟超时，写入 AgentRunSpec；
        # 对齐 nanobot _sync_subagent_runtime_limits（仅超时同步，model/runtime 推迟）。
        sess_key = origin.get("session_key") if origin else None
        llm_timeout = (
            self._llm_wall_timeout_for_session(sess_key)
            if self._llm_wall_timeout_for_session
            else None
        )
        # step121：子代理工具集每次 spawn 新建（对齐 nanobot per-spawn _build_tools）；
        # 同一实例复用于 AgentRunSpec 与治理配置，避免重复构建。
        tools = self._build_tools()
        # step124（G10）：用 checkpoint_callback 把 runner 迭代相位同步到 status.phase；
        # runner 已在每轮迭代发出 awaiting_tools / tools_completed / final_response 等相位。
        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                # step122（G5）：provider 沿用 manager 自身（生产 == runtime.provider）；
                # model / temperature / max_tokens 继承父会话 runtime，缺省退化为标量缺省。
                # 用 getattr 防御 runtime 未实现这些属性的情况（如测试传入的占位对象）。
                provider=provider,
                model=getattr(runtime, "model", None) or None,
                temperature=getattr(runtime, "temperature", 0.7),
                max_tokens=getattr(runtime, "max_tokens", 4096),
                max_iterations=self.max_iterations,
                hook=_SubagentHook(task_id, status),
                # step124（G10）：runner 每轮迭代回调，驱动多相位状态
                checkpoint_callback=_on_checkpoint,
                request_context=req_ctx,
                workspace_scope=ws_scope,
                llm_timeout_s=llm_timeout,
                # step120：对齐 nanobot 子代理，传播父配置的运行限制
                governance_config=ContextGovernanceConfig(
                    tools=tools,
                    max_tool_result_chars=self._max_tool_result_chars,
                    # 保持与 runner._resolve_gov_config 默认一致的上下文预算，
                    # 避免 context_window_tokens=None 触发全量工具结果摘要（改变既有行为）。
                    context_window_tokens=200_000,
                    max_tokens=4096,
                ),
                fail_on_tool_error=self._fail_on_tool_error,
                # 子代理 max-iterations 由隐形续跑接管（对齐 nanobot 硬编码 False）
                finalize_on_max_iterations=False,
                max_iterations_message="Task completed but no final response was generated.",
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
        # step124：对齐 nanobot，用模板渲染 announce（而非内联 f-string）
        status_text = "completed successfully" if status == "ok" else "failed"
        content = _render_subagent_announce(label, status_text, task, result)
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata = {"injected_event": "subagent_result", "subagent_task_id": task_id}
        # step124：对齐 nanobot，透传 origin_message_id 到 announce 元数据
        origin_message_id = origin.get("origin_message_id") if origin else None
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
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

    def get_task_statuses(self) -> list[dict[str, Any]]:
        """返回所有子代理任务状态的快照（step124，对齐 nanobot self/my 可观测）。

        供 ``MyTool`` 的 ``subagents`` key 读取，使父代理可查询运行中的子代理；
        用 ``dataclasses.asdict`` 转为可 JSON 序列化的纯 dict 列表，不暴露管理器内部。

        Returns:
            每个 ``SubagentStatus`` 的 ``asdict`` 结果列表（空集时返回空列表）。
        """
        return [asdict(status) for status in self._task_statuses.values()]
