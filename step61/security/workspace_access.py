"""Workspace 访问范围与 sandbox 能力助手。

对齐 nanobot `security/workspace_access.py` 的最小集（A10）：
- ``WorkspaceScope``：单个 agent turn 生效的"项目根 + 访问模式"不可变快照；
- ``WorkspaceScopeResolver``：在 turn 边界解析有效 scope（默认值 / 消息级
  metadata 覆盖）；
- ``_CURRENT_WORKSPACE_SCOPE`` ContextVar：绑定供工具查询（``current_tool_workspace``）；
- ``current_scope_allows_loopback``：WebUI Full Access 轮次的 loopback 门禁。

与 nanobot 的差异（刻意简化）：
- 无 WebUI 场景：``scoped_channel`` 默认仍是 ``"websocket"``（对齐 nanobot 语义，
  便于未来接入真实通道时行为一致），lean 的 CLI 通道走 ``default()`` 分支；
- sandbox 探测只认 ``NANOBOT_WORKSPACE_SANDBOX_ENFORCED`` /
  ``NANOBOT_WORKSPACE_SANDBOX_PROVIDER`` 两个 env（不做 macOS/bwrap 平台探测）。
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

WorkspaceAccessMode = Literal["restricted", "full"]
WORKSPACE_SCOPE_METADATA_KEY = "workspace_scope"
_ACCESS_MODES = {"restricted", "full"}

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", ""}
_PROVIDER_LABELS = {
    "none": "None",
    "unknown": "Unknown system sandbox",
    "macos_app_sandbox": "macOS App Sandbox",
    "bwrap": "Bubblewrap",
}

# 当前 turn 生效的 workspace scope；无绑定（如进程外代码）时为 None。
_CURRENT_WORKSPACE_SCOPE: ContextVar["WorkspaceScope | None"] = ContextVar(
    "nanobot_workspace_scope",
    default=None,
)


class WorkspaceScopeError(ValueError):
    """请求的 workspace scope 非法时抛出（对齐 nanobot 的 400 语义）。"""

    status = 400

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class WorkspaceSandboxStatus:
    """已解析的 workspace sandbox 状态（供运行时展示与工具消费）。"""

    restrict_to_workspace: bool
    workspace_root: str
    level: str
    enforced: bool
    provider: str
    provider_label: str
    summary: str

    def as_dict(self) -> dict[str, object]:
        """转 dict（对齐 nanobot payload 字段名）。"""
        return {
            "restrict_to_workspace": self.restrict_to_workspace,
            "workspace_root": self.workspace_root,
            "level": self.level,
            "enforced": self.enforced,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class WorkspaceScope:
    """单个 agent turn 的"项目根 + 访问模式"不可变快照。

    Attributes:
        project_path: 解析后的项目根目录（绝对路径）。
        access_mode: ``restricted``（限 workspace）或 ``full``（不限制）。
        restrict_to_workspace: 是否限制工具访问在 workspace 内。
        sandbox_status: 当前宿主如何执行该限制（off/system/application）。
        source_channel: 产生该 scope 的通道名（``None`` 表示默认）。
    """

    project_path: Path
    access_mode: WorkspaceAccessMode
    restrict_to_workspace: bool
    sandbox_status: WorkspaceSandboxStatus
    source_channel: str | None = None

    @property
    def project_name(self) -> str:
        """项目名（目录名兜底完整路径）。"""
        return self.project_path.name or str(self.project_path)

    def metadata(self) -> dict[str, str]:
        """可持久化的最小 metadata 字典。"""
        return {
            "project_path": str(self.project_path),
            "access_mode": self.access_mode,
        }

    def payload(self) -> dict[str, Any]:
        """完整展示载荷（含 sandbox 状态）。"""
        return {
            **self.metadata(),
            "project_name": self.project_name,
            "restrict_to_workspace": self.restrict_to_workspace,
            "sandbox_status": self.sandbox_status.as_dict(),
        }


@dataclass(frozen=True)
class ToolWorkspace:
    """某个工具调用解析到的 workspace 策略。

    Attributes:
        project_path: 当前项目的根目录（无则 None）。
        restrict_to_workspace: 本次调用是否受限。
        scope: 产生该策略的完整 WorkspaceScope（无绑定则为 None）。
    """

    project_path: Path | None
    restrict_to_workspace: bool
    scope: WorkspaceScope | None = None

    @property
    def allowed_root(self) -> Path | None:
        """受限时返回允许的根目录（即项目根），否则 None（表示不限制）。"""
        if self.restrict_to_workspace and self.project_path is not None:
            return self.project_path
        return None


@dataclass(frozen=True)
class WorkspaceScopeResolver:
    """在 agent turn 边界解析有效 workspace scope。

    Attributes:
        default_workspace: 默认项目根。
        default_restrict_to_workspace: 默认是否限制在 workspace 内。
        scoped_channel: 只有该通道的消息才支持按 metadata 覆盖 scope；
            其它通道一律走 ``default()``。
    """

    default_workspace: str | Path
    default_restrict_to_workspace: bool
    scoped_channel: str = "websocket"

    @property
    def sandbox_status(self) -> WorkspaceSandboxStatus:
        """默认 scope 的 sandbox 状态。"""
        return self.default().sandbox_status

    def default(self) -> WorkspaceScope:
        """返回默认 scope（不读消息 metadata）。"""
        return default_workspace_scope(
            self.default_workspace,
            self.default_restrict_to_workspace,
        )

    def for_message(
        self,
        msg: Any,
        session_metadata: Any,
    ) -> WorkspaceScope:
        """按入站消息解析 scope：消息/会话 metadata 可覆盖项目根与访问模式。"""
        return self.for_turn(
            channel=getattr(msg, "channel", None),
            message_metadata=getattr(msg, "metadata", None),
            session_metadata=session_metadata,
        )

    def for_turn(
        self,
        *,
        channel: str | None,
        message_metadata: Any,
        session_metadata: Any,
    ) -> WorkspaceScope:
        """按通道 + metadata 解析 scope；非 scoped 通道一律走默认值。"""
        if channel != self.scoped_channel:
            return self.default()
        return resolve_effective_workspace_scope(
            message_metadata=message_metadata,
            session_metadata=session_metadata,
            default_workspace=self.default_workspace,
            default_restrict_to_workspace=self.default_restrict_to_workspace,
            source_channel=channel,
        )

    def persist_message_scope(self, session: Any, msg: Any) -> None:
        """把 scoped 通道消息声明的 scope 落到会话 metadata（供后续轮次复用）。"""
        if getattr(msg, "channel", None) != self.scoped_channel:
            return
        metadata = getattr(msg, "metadata", None)
        if not isinstance(metadata, dict):
            return
        raw = metadata.get(WORKSPACE_SCOPE_METADATA_KEY)
        if isinstance(raw, dict):
            session.metadata[WORKSPACE_SCOPE_METADATA_KEY] = dict(raw)


def workspace_sandbox_status(
    *,
    restrict_to_workspace: bool,
    workspace: str | Path,
    environ: dict[str, str] | None = None,
) -> WorkspaceSandboxStatus:
    """返回当前宿主如何执行 workspace 限制。

    三级状态：
    - ``off``：未开启限制；
    - ``system``：由外部 sandbox（bwrap/macOS App Sandbox）系统级强制；
    - ``application``：仅 nanobot 应用级守卫（lean 默认形态）。
    """
    workspace_root = str(Path(workspace).expanduser().resolve(strict=False))
    provider = _env_system_provider(environ)
    if not restrict_to_workspace:
        return WorkspaceSandboxStatus(
            restrict_to_workspace=False,
            workspace_root=workspace_root,
            level="off",
            enforced=False,
            provider="none",
            provider_label=_provider_label("none"),
            summary="Workspace restriction is disabled.",
        )

    if provider:
        label = _provider_label(provider)
        return WorkspaceSandboxStatus(
            restrict_to_workspace=True,
            workspace_root=workspace_root,
            level="system",
            enforced=True,
            provider=provider,
            provider_label=label,
            summary=f"Workspace restriction is system-enforced by {label}.",
        )

    return WorkspaceSandboxStatus(
        restrict_to_workspace=True,
        workspace_root=workspace_root,
        level="application",
        enforced=False,
        provider="none",
        provider_label=_provider_label("none"),
        summary="Workspace restriction uses nanobot application-level guards.",
    )


def default_access_mode(restrict_to_workspace: bool) -> WorkspaceAccessMode:
    """由 restrict 布尔反推访问模式：受限 → restricted，否则 full。"""
    return "restricted" if restrict_to_workspace else "full"


def build_workspace_scope(
    project_path: str | Path,
    access_mode: str,
    *,
    source_channel: str | None = None,
) -> WorkspaceScope:
    """按项目根 + 访问模式构造 WorkspaceScope（内部规范化路径与模式）。"""
    mode = _normalize_access_mode(access_mode)
    root = Path(project_path).expanduser().resolve(strict=False)
    restrict = mode == "restricted"
    return WorkspaceScope(
        project_path=root,
        access_mode=mode,
        restrict_to_workspace=restrict,
        sandbox_status=workspace_sandbox_status(
            restrict_to_workspace=restrict,
            workspace=root,
        ),
        source_channel=source_channel,
    )


def default_workspace_scope(
    workspace: str | Path,
    restrict_to_workspace: bool,
    *,
    source_channel: str | None = None,
) -> WorkspaceScope:
    """按默认 workspace + restrict 布尔构造默认 scope。"""
    return build_workspace_scope(
        workspace,
        default_access_mode(restrict_to_workspace),
        source_channel=source_channel,
    )


def validate_workspace_scope_payload(
    raw: Any,
    *,
    default_workspace: str | Path,
    default_restrict_to_workspace: bool,
    source_channel: str | None = None,
) -> WorkspaceScope:
    """校验客户端请求的 workspace scope（对齐 nanobot 的 WebUI 载荷语义）。"""
    if raw is None:
        return default_workspace_scope(
            default_workspace,
            default_restrict_to_workspace,
            source_channel=source_channel,
        )
    if not isinstance(raw, dict):
        raise WorkspaceScopeError("workspace_scope must be an object")

    raw_path = raw.get("project_path") or raw.get("path")
    if raw_path is None or raw_path == "":
        raw_path = str(Path(default_workspace).expanduser().resolve(strict=False))
    if not isinstance(raw_path, str):
        raise WorkspaceScopeError("project_path must be a string")
    if "\0" in raw_path:
        raise WorkspaceScopeError("project_path contains invalid characters")

    project = Path(raw_path).expanduser()
    if not project.is_absolute():
        raise WorkspaceScopeError("project_path must be absolute")
    project = project.resolve(strict=False)
    if not project.is_dir():
        raise WorkspaceScopeError("project_path must be an existing directory")

    raw_mode = raw.get("access_mode")
    if raw_mode is None:
        raw_mode = default_access_mode(default_restrict_to_workspace)
    if not isinstance(raw_mode, str):
        raise WorkspaceScopeError("access_mode must be a string")
    return build_workspace_scope(project, raw_mode, source_channel=source_channel)


def workspace_scope_from_metadata(
    metadata: Any,
    *,
    default_workspace: str | Path,
    default_restrict_to_workspace: bool,
    source_channel: str | None = None,
) -> WorkspaceScope:
    """从持久化 metadata 解析 scope；旧/损坏数据安全回退到默认值。"""
    if not isinstance(metadata, dict):
        return default_workspace_scope(
            default_workspace,
            default_restrict_to_workspace,
            source_channel=source_channel,
        )
    try:
        return validate_workspace_scope_payload(
            metadata.get(WORKSPACE_SCOPE_METADATA_KEY),
            default_workspace=default_workspace,
            default_restrict_to_workspace=default_restrict_to_workspace,
            source_channel=source_channel,
        )
    except WorkspaceScopeError:
        return default_workspace_scope(
            default_workspace,
            default_restrict_to_workspace,
            source_channel=source_channel,
        )


def resolve_effective_workspace_scope(
    *,
    message_metadata: Any,
    session_metadata: Any,
    default_workspace: str | Path,
    default_restrict_to_workspace: bool,
    source_channel: str | None = None,
) -> WorkspaceScope:
    """按消息 metadata 优先、会话 metadata 兜底解析有效 scope。"""
    if isinstance(message_metadata, dict) and WORKSPACE_SCOPE_METADATA_KEY in message_metadata:
        return workspace_scope_from_metadata(
            message_metadata,
            default_workspace=default_workspace,
            default_restrict_to_workspace=default_restrict_to_workspace,
            source_channel=source_channel,
        )
    return workspace_scope_from_metadata(
        session_metadata,
        default_workspace=default_workspace,
        default_restrict_to_workspace=default_restrict_to_workspace,
        source_channel=source_channel,
    )


def bind_workspace_scope(scope: WorkspaceScope) -> Token[WorkspaceScope | None]:
    """把 scope 绑定到当前 async 上下文；返回 token 供 ``reset_workspace_scope`` 使用。"""
    return _CURRENT_WORKSPACE_SCOPE.set(scope)


def reset_workspace_scope(token: Token[WorkspaceScope | None]) -> None:
    """恢复上一次绑定的 scope（token 来自 ``bind_workspace_scope``）。"""
    _CURRENT_WORKSPACE_SCOPE.reset(token)


def current_workspace_scope() -> WorkspaceScope | None:
    """返回当前 turn 绑定的 scope；未绑定（进程外代码）返回 None。"""
    return _CURRENT_WORKSPACE_SCOPE.get()


def current_tool_workspace(
    default_workspace: str | Path | None,
    *,
    restrict_to_workspace: bool = False,
    sandbox_restricts_workspace: bool = False,
) -> ToolWorkspace:
    """工具侧查询入口：返回当前调用的 workspace/访问策略。

    有 ContextVar 绑定（turn 内）时以绑定为准；否则回退到构造参数。
    ``sandbox_restricts_workspace`` 表示宿主 sandbox 已系统级限制，
    即使策略意图未开启也要视为受限。
    """
    scope = current_workspace_scope()
    project_path = (
        scope.project_path
        if scope is not None
        else Path(default_workspace).expanduser() if default_workspace is not None else None
    )
    restrict = (
        scope.restrict_to_workspace
        if scope is not None
        else bool(restrict_to_workspace)
    ) or sandbox_restricts_workspace
    return ToolWorkspace(
        project_path=project_path,
        restrict_to_workspace=restrict,
        scope=scope,
    )


def current_scope_allows_loopback(*, enabled: bool) -> bool:
    """loopback 门禁：仅当显式放行且当前为 WebUI Full Access 轮次才允许。

    判定条件（对齐 nanobot）：scope 存在、来源通道为 ``websocket``、
    access_mode 为 ``full`` 且未 restrict —— 三个条件同时满足才放行 loopback。
    """
    scope = current_workspace_scope()
    return bool(
        enabled
        and scope is not None
        and scope.source_channel == "websocket"
        and scope.access_mode == "full"
        and not scope.restrict_to_workspace
    )


def _env_system_provider(environ: dict[str, str] | None = None) -> str | None:
    """从环境变量探测系统 sandbox provider（未声明/显式关闭返回 None）。"""
    env = environ if environ is not None else os.environ
    explicit_provider = env.get("NANOBOT_WORKSPACE_SANDBOX_PROVIDER")
    enforced = env.get("NANOBOT_WORKSPACE_SANDBOX_ENFORCED")

    if enforced is None:
        return None
    normalized_marker = enforced.strip().lower()
    if normalized_marker in _FALSE_VALUES:
        return None
    if normalized_marker in _TRUE_VALUES:
        return _normalize_provider(explicit_provider)
    return _normalize_provider(normalized_marker)


def _normalize_provider(value: str | None) -> str:
    """规范化 provider 名（小写、下划线）。"""
    if not value:
        return "unknown"
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or "unknown"


def _provider_label(provider: str) -> str:
    """provider 名 → 展示标签。"""
    if provider in _PROVIDER_LABELS:
        return _PROVIDER_LABELS[provider]
    return provider.replace("_", " ").title()


def _normalize_access_mode(value: str) -> WorkspaceAccessMode:
    """规范化访问模式：restrict/full-access 等别名收敛为两值之一。"""
    mode = value.strip().lower().replace("_", "-")
    if mode == "restrict":
        mode = "restricted"
    if mode == "full-access":
        mode = "full"
    if mode not in _ACCESS_MODES:
        raise WorkspaceScopeError("access_mode must be restricted or full")
    return mode  # type: ignore[return-value]
