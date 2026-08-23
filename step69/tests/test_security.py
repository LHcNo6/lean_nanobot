"""step29 安全模块测试（A10 + H7 子集）。

全构造数据：tmp_path 真实文件 + mock ``socket.getaddrinfo``，不触碰真实
网络 / API Key / 环境变量。
"""

from __future__ import annotations

import socket as _socket
from pathlib import Path
from unittest import mock

import pytest

from step69.security import network
from step69.security import workspace_policy as wp
from step69.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScopeError,
    WorkspaceScopeResolver,
    bind_workspace_scope,
    build_workspace_scope,
    current_scope_allows_loopback,
    current_tool_workspace,
    current_workspace_scope,
    default_access_mode,
    default_workspace_scope,
    reset_workspace_scope,
    validate_workspace_scope_payload,
    workspace_scope_from_metadata,
    workspace_sandbox_status,
)
from step69.security.workspace_policy import WorkspaceBoundaryError

DNS = {
    "example.com": "93.184.216.34",
    "localhost": "127.0.0.1",
    "internal.local": "192.168.1.10",
    "metadata.local": "169.254.169.254",
    "cg.local": "100.64.0.1",
}


def _patch_dns(monkeypatch, mapping: dict[str, str]) -> None:
    """把 ``socket.getaddrinfo`` 打桩为 host -> ip 映射表（不碰真实 DNS）。"""

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        ip = mapping.get(str(host))
        if ip is None:
            raise _socket.gaierror(f"Cannot resolve hostname: {host}")
        af = _socket.AF_INET6 if ":" in ip else _socket.AF_INET
        sockaddr = (ip, port or 0, 0, 0) if af == _socket.AF_INET6 else (ip, port or 0)
        return [(af, type or 1, proto, "", sockaddr)]

    monkeypatch.setattr(network.socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# workspace_access：scope 解析与规范化
# ---------------------------------------------------------------------------


class TestWorkspaceScopeBasics:
    def test_default_scope_restricted(self, tmp_path):
        scope = default_workspace_scope(tmp_path, True)
        assert scope.access_mode == "restricted"
        assert scope.restrict_to_workspace is True
        assert scope.project_path == Path(tmp_path).resolve()
        assert scope.sandbox_status.level == "application"
        assert scope.sandbox_status.restrict_to_workspace is True

    def test_default_scope_full(self, tmp_path):
        scope = default_workspace_scope(tmp_path, False)
        assert scope.access_mode == "full"
        assert scope.restrict_to_workspace is False
        assert scope.sandbox_status.level == "off"

    def test_default_access_mode_mapping(self):
        assert default_access_mode(True) == "restricted"
        assert default_access_mode(False) == "full"

    def test_build_scope_normalizes_mode_aliases(self, tmp_path):
        assert build_workspace_scope(tmp_path, "restrict").access_mode == "restricted"
        assert build_workspace_scope(tmp_path, "full-access").access_mode == "full"

    def test_build_scope_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(WorkspaceScopeError):
            build_workspace_scope(tmp_path, "sudo")

    def test_scope_payload_and_metadata(self, tmp_path):
        scope = default_workspace_scope(tmp_path, True, source_channel="cli")
        meta = scope.metadata()
        assert meta["access_mode"] == "restricted"
        assert meta["project_path"] == str(Path(tmp_path).resolve())
        payload = scope.payload()
        assert payload["project_name"] == tmp_path.name
        assert payload["restrict_to_workspace"] is True
        assert payload["sandbox_status"]["level"] == "application"

    def test_scope_project_name_fallback(self, tmp_path):
        scope = default_workspace_scope(tmp_path, False)
        assert scope.project_name == tmp_path.name


# ---------------------------------------------------------------------------
# workspace_access：sandbox 状态与 env 探测
# ---------------------------------------------------------------------------


class TestSandboxStatus:
    def test_sandbox_off(self, tmp_path):
        status = workspace_sandbox_status(restrict_to_workspace=False, workspace=tmp_path)
        assert status.level == "off"
        assert status.enforced is False
        assert status.provider == "none"

    def test_sandbox_application(self, tmp_path):
        status = workspace_sandbox_status(restrict_to_workspace=True, workspace=tmp_path)
        assert status.level == "application"
        assert status.enforced is False

    def test_sandbox_system_provider_env(self, tmp_path):
        status = workspace_sandbox_status(
            restrict_to_workspace=True,
            workspace=tmp_path,
            environ={"NANOBOT_WORKSPACE_SANDBOX_ENFORCED": "1",
                     "NANOBOT_WORKSPACE_SANDBOX_PROVIDER": "bwrap"},
        )
        assert status.level == "system"
        assert status.enforced is True
        assert status.provider == "bwrap"

    def test_sandbox_env_marker_as_provider(self, tmp_path):
        status = workspace_sandbox_status(
            restrict_to_workspace=True,
            workspace=tmp_path,
            environ={"NANOBOT_WORKSPACE_SANDBOX_ENFORCED": "bwrap"},
        )
        assert status.provider == "bwrap"

    def test_sandbox_env_explicit_false(self, tmp_path):
        status = workspace_sandbox_status(
            restrict_to_workspace=True,
            workspace=tmp_path,
            environ={"NANOBOT_WORKSPACE_SANDBOX_ENFORCED": "false"},
        )
        assert status.level == "application"
        assert status.provider == "none"


# ---------------------------------------------------------------------------
# workspace_access：metadata 载荷解析与 resolver
# ---------------------------------------------------------------------------


class TestScopePayloadValidation:
    def test_none_falls_back_to_default(self, tmp_path):
        scope = validate_workspace_scope_payload(
            None, default_workspace=tmp_path, default_restrict_to_workspace=True,
        )
        assert scope.access_mode == "restricted"

    def test_valid_payload(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        scope = validate_workspace_scope_payload(
            {"project_path": str(project), "access_mode": "full"},
            default_workspace=tmp_path, default_restrict_to_workspace=True,
        )
        assert scope.project_path == project.resolve()
        assert scope.access_mode == "full"

    def test_rejects_relative_path(self, tmp_path):
        with pytest.raises(WorkspaceScopeError):
            validate_workspace_scope_payload(
                {"project_path": "relative/dir"}, default_workspace=tmp_path,
                default_restrict_to_workspace=False,
            )

    def test_rejects_missing_directory(self, tmp_path):
        with pytest.raises(WorkspaceScopeError):
            validate_workspace_scope_payload(
                {"project_path": str(tmp_path / "nope")}, default_workspace=tmp_path,
                default_restrict_to_workspace=False,
            )

    def test_rejects_invalid_mode(self, tmp_path):
        with pytest.raises(WorkspaceScopeError):
            validate_workspace_scope_payload(
                {"access_mode": 42}, default_workspace=tmp_path,
                default_restrict_to_workspace=False,
            )

    def test_from_metadata_stale_data_falls_back(self, tmp_path):
        scope = workspace_scope_from_metadata(
            {"workspace_scope": "garbage"}, default_workspace=tmp_path,
            default_restrict_to_workspace=True,
        )
        assert scope.project_path == Path(tmp_path).resolve()

    def test_from_metadata_valid(self, tmp_path):
        scope = workspace_scope_from_metadata(
            {"workspace_scope": {"project_path": str(tmp_path), "access_mode": "full"}},
            default_workspace=tmp_path, default_restrict_to_workspace=True,
        )
        assert scope.access_mode == "full"


class TestWorkspaceScopeResolver:
    def test_default_scope(self, tmp_path):
        resolver = WorkspaceScopeResolver(tmp_path, default_restrict_to_workspace=True)
        scope = resolver.for_turn(channel="cli", message_metadata={}, session_metadata=None)
        assert scope.restrict_to_workspace is True
        assert scope.project_path == Path(tmp_path).resolve()

    def test_non_scoped_channel_ignores_metadata(self, tmp_path):
        resolver = WorkspaceScopeResolver(tmp_path, default_restrict_to_workspace=False)
        meta = {WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(tmp_path), "access_mode": "restricted"}}
        scope = resolver.for_turn(channel="cli", message_metadata=meta, session_metadata=None)
        assert scope.access_mode == "full"

    def test_scoped_channel_honors_message_metadata(self, tmp_path):
        resolver = WorkspaceScopeResolver(tmp_path, default_restrict_to_workspace=False)
        meta = {WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(tmp_path), "access_mode": "restricted"}}
        scope = resolver.for_turn(
            channel="websocket", message_metadata=meta, session_metadata=None,
        )
        assert scope.access_mode == "restricted"

    def test_scoped_channel_falls_back_to_session_metadata(self, tmp_path):
        resolver = WorkspaceScopeResolver(tmp_path, default_restrict_to_workspace=False)
        session_meta = {WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(tmp_path), "access_mode": "restricted"}}
        scope = resolver.for_turn(
            channel="websocket", message_metadata={}, session_metadata=session_meta,
        )
        assert scope.access_mode == "restricted"

    def test_persist_message_scope_only_scoped_channel(self, tmp_path):
        resolver = WorkspaceScopeResolver(tmp_path, default_restrict_to_workspace=False)
        project = tmp_path / "proj"
        project.mkdir()
        payload = {WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(project), "access_mode": "restricted"}}
        session = mock.Mock(metadata={})
        resolver.persist_message_scope(session, mock.Mock(channel="cli", metadata=payload))
        assert WORKSPACE_SCOPE_METADATA_KEY not in session.metadata
        resolver.persist_message_scope(session, mock.Mock(channel="websocket", metadata=payload))
        assert session.metadata[WORKSPACE_SCOPE_METADATA_KEY]["access_mode"] == "restricted"


# ---------------------------------------------------------------------------
# workspace_access：ContextVar 绑定 + 工具查询
# ---------------------------------------------------------------------------


class TestContextVarBinding:
    def test_default_none(self):
        assert current_workspace_scope() is None

    def test_bind_reset(self, tmp_path):
        scope = default_workspace_scope(tmp_path, True)
        token = bind_workspace_scope(scope)
        try:
            assert current_workspace_scope() is scope
        finally:
            reset_workspace_scope(token)
        assert current_workspace_scope() is None

    def test_bind_nested_restore(self, tmp_path):
        outer = default_workspace_scope(tmp_path, False)
        inner = default_workspace_scope(tmp_path, True)
        t1 = bind_workspace_scope(outer)
        t2 = bind_workspace_scope(inner)
        assert current_workspace_scope() is inner
        reset_workspace_scope(t2)
        assert current_workspace_scope() is outer
        reset_workspace_scope(t1)

    def test_current_tool_workspace_unbound_fallback(self, tmp_path):
        access = current_tool_workspace(tmp_path, restrict_to_workspace=True)
        assert access.project_path == Path(tmp_path).expanduser()
        assert access.restrict_to_workspace is True
        assert access.allowed_root == Path(tmp_path).expanduser()

    def test_current_tool_workspace_unbound_full(self, tmp_path):
        access = current_tool_workspace(tmp_path, restrict_to_workspace=False)
        assert access.allowed_root is None

    def test_current_tool_workspace_prefers_binding(self, tmp_path):
        scope = default_workspace_scope(tmp_path, True)
        token = bind_workspace_scope(scope)
        try:
            access = current_tool_workspace("", restrict_to_workspace=False)
            assert access.project_path == scope.project_path
            assert access.restrict_to_workspace is True
        finally:
            reset_workspace_scope(token)

    def test_sandbox_restricts_workspace(self, tmp_path):
        access = current_tool_workspace(
            tmp_path, restrict_to_workspace=False, sandbox_restricts_workspace=True,
        )
        assert access.restrict_to_workspace is True


class TestLoopbackGate:
    def test_closed_without_binding(self):
        assert current_scope_allows_loopback(enabled=True) is False

    def test_closed_when_disabled(self, tmp_path):
        scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
        token = bind_workspace_scope(scope)
        try:
            assert current_scope_allows_loopback(enabled=False) is False
        finally:
            reset_workspace_scope(token)

    def test_closed_for_non_websocket_channel(self, tmp_path):
        scope = build_workspace_scope(tmp_path, "full", source_channel="cli")
        token = bind_workspace_scope(scope)
        try:
            assert current_scope_allows_loopback(enabled=True) is False
        finally:
            reset_workspace_scope(token)

    def test_closed_for_restricted_access(self, tmp_path):
        scope = build_workspace_scope(tmp_path, "restricted", source_channel="websocket")
        token = bind_workspace_scope(scope)
        try:
            assert current_scope_allows_loopback(enabled=True) is False
        finally:
            reset_workspace_scope(token)

    def test_open_for_full_websocket(self, tmp_path):
        scope = build_workspace_scope(tmp_path, "full", source_channel="websocket")
        token = bind_workspace_scope(scope)
        try:
            assert current_scope_allows_loopback(enabled=True) is True
        finally:
            reset_workspace_scope(token)


# ---------------------------------------------------------------------------
# workspace_policy：路径边界守卫
# ---------------------------------------------------------------------------


class TestWorkspacePolicy:
    def test_resolve_relative_against_workspace(self, tmp_path):
        resolved = wp.resolve_path("a/b.txt", tmp_path)
        assert resolved == Path(tmp_path).resolve() / "a/b.txt"

    def test_resolve_absolute_unchanged(self, tmp_path):
        target = tmp_path / "a.txt"
        assert wp.resolve_path(str(target), tmp_path) == target.resolve()

    def test_is_path_within_true_and_false(self, tmp_path):
        inside = tmp_path / "sub" / "file.txt"
        outside = tmp_path.parent / "other.txt"
        assert wp.is_path_within(inside, tmp_path) is True
        assert wp.is_path_within(outside, tmp_path) is False
        assert wp.is_path_within(tmp_path, tmp_path) is True

    def test_is_path_allowed_any_root(self, tmp_path):
        a = tmp_path / "a"
        assert wp.is_path_allowed(tmp_path / "x", [a]) is False
        assert wp.is_path_allowed(a / "x", [a]) is True

    def test_require_path_within_ok(self, tmp_path):
        f = tmp_path / "ok.txt"
        assert wp.require_path_within(f, tmp_path) == f.resolve()

    def test_require_path_within_raises(self, tmp_path):
        outside = tmp_path.parent / "evil.txt"
        with pytest.raises(WorkspaceBoundaryError):
            wp.require_path_within(outside, tmp_path)

    def test_require_path_within_boundary_note(self, tmp_path):
        outside = tmp_path.parent / "evil.txt"
        try:
            wp.require_path_within(outside, tmp_path)
        except WorkspaceBoundaryError as exc:
            assert "hard policy boundary" in str(exc)
        else:
            raise AssertionError("expected WorkspaceBoundaryError")

    def test_resolve_allowed_path_no_boundary(self, tmp_path):
        outside = tmp_path.parent / "f.txt"
        assert wp.resolve_allowed_path(str(outside)) == outside.resolve()

    def test_resolve_allowed_path_inside(self, tmp_path):
        f = tmp_path / "in.txt"
        assert wp.resolve_allowed_path(f, allowed_root=tmp_path) == f.resolve()

    def test_resolve_allowed_path_outside_raises(self, tmp_path):
        outside = tmp_path.parent / "f.txt"
        with pytest.raises(WorkspaceBoundaryError):
            wp.resolve_allowed_path(outside, workspace=tmp_path, allowed_root=tmp_path)

    def test_resolve_allowed_path_extra_roots(self, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        f = other / "g.txt"
        assert wp.resolve_allowed_path(
            f, workspace=tmp_path, allowed_root=tmp_path,
            extra_allowed_roots=[other],
        ) == f.resolve()

    def test_resolve_allowed_path_extra_files(self, tmp_path):
        special = tmp_path.parent / "special.txt"
        special.touch()
        assert wp.resolve_allowed_path(
            special, workspace=tmp_path, allowed_root=tmp_path,
            extra_allowed_files=[special],
        ) == special.resolve()


# ---------------------------------------------------------------------------
# network：SSRF / loopback 校验（mock getaddrinfo，不碰真实 DNS）
# ---------------------------------------------------------------------------


class TestNetworkSSRF:
    def test_is_loopback_host(self):
        assert network.is_loopback_host("localhost") is True
        assert network.is_loopback_host("127.0.0.1") is True
        assert network.is_loopback_host("[::1]") is True
        assert network.is_loopback_host("example.com") is False

    def test_scheme_restriction(self, monkeypatch):
        ok, error = network.validate_url_target("ftp://example.com/file")
        assert ok is False
        assert "Only http/https" in error

    def test_missing_hostname(self, monkeypatch):
        ok, error = network.validate_url_target("http://")
        assert ok is False

    def test_public_url_allowed(self, monkeypatch):
        _patch_dns(monkeypatch, DNS)
        ok, error, ips = network.resolve_url_target("http://example.com/path")
        assert ok is True
        assert ips == ("93.184.216.34",)

    def test_private_resolution_blocked(self, monkeypatch):
        _patch_dns(monkeypatch, DNS)
        ok, error = network.validate_url_target("http://internal.local/")
        assert ok is False
        assert "private/internal" in error

    def test_linklocal_metadata_blocked(self, monkeypatch):
        _patch_dns(monkeypatch, DNS)
        ok, _ = network.validate_url_target("http://metadata.local/")
        assert ok is False

    def test_loopback_blocked_unless_allowed(self, monkeypatch):
        _patch_dns(monkeypatch, DNS)
        ok, error = network.validate_url_target("http://localhost:8080/")
        assert ok is False
        assert "private/internal" in error
        ok, _ = network.validate_url_target("http://localhost:8080/", allow_loopback=True)
        assert ok is True

    def test_allow_loopback_narrow_for_public_hostname(self, monkeypatch):
        # 公网域名解析到 loopback 也不能放行（窄语义）。
        monkeypatch.setattr(
            network.socket, "getaddrinfo",
            lambda host, port, family=0, type=0, proto=0, flags=0: [
                (_socket.AF_INET, type or 1, proto, "", ("127.0.0.1", port or 0)),
            ],
        )
        ok, error = network.validate_url_target("http://example.com/", allow_loopback=True)
        assert ok is False
        assert "private/internal" in error

    def test_cgn_range_blocked_and_whitelistable(self, monkeypatch):
        _patch_dns(monkeypatch, DNS)
        ok, _ = network.validate_url_target("http://cg.local/")
        assert ok is False
        network.configure_ssrf_whitelist(["100.64.0.0/10"])
        try:
            ok, _ = network.validate_url_target("http://cg.local/")
            assert ok is True
        finally:
            network.configure_ssrf_whitelist([])

    def test_ipv6_mapped_ipv4_normalized(self, monkeypatch):
        # ::ffff:127.0.0.1 应被识别为 loopback（规整后拦截）
        monkeypatch.setattr(
            network.socket, "getaddrinfo",
            lambda host, port, family=0, type=0, proto=0, flags=0: [
                (_socket.AF_INET6, type or 1, proto, "", ("::ffff:127.0.0.1", port or 0, 0, 0)),
            ],
        )
        ok, _ = network.validate_url_target("http://example.com/")
        assert ok is False

    def test_contains_internal_url(self, monkeypatch):
        _patch_dns(monkeypatch, DNS)
        assert network.contains_internal_url("curl http://example.com/") is False
        assert network.contains_internal_url("curl http://internal.local/") is True
        assert network.contains_internal_url("ls -la") is False
