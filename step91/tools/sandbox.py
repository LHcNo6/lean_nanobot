"""沙箱后端：shell 命令执行的沙箱包装（step89）。

对齐 nanobot `agent/tools/sandbox.py` 的最小子集：
- 函数注册机制（``_BACKENDS`` 字典）；
- ``none`` 后端：不包装（默认）；
- ``bwrap`` 后端：bubblewrap 沙箱（Linux）；
- ``wrap_command`` 入口函数。

新增后端只需实现 ``_<name>(command, workspace, cwd) -> str`` 并注册到 ``_BACKENDS``。
"""

from __future__ import annotations

import shlex
from pathlib import Path


def _none(command: str, workspace: str, cwd: str) -> str:
    """无沙箱后端：直接返回原命令。

    Args:
        command: 原始 shell 命令。
        workspace: workspace 路径（未使用）。
        cwd: 当前工作目录（未使用）。

    Returns:
        原始命令字符串。
    """
    return command


def _bwrap(command: str, workspace: str, cwd: str) -> str:
    """bubblewrap 沙箱后端（Linux only）。

    用 bwrap 包装命令，限制文件系统访问：
    - 只读绑定系统目录（/usr, /bin, /lib 等）；
    - /proc, /dev, /tmp 独立；
    - workspace 读写绑定；
    - 隐藏 workspace 父目录（防止访问 config.json）；
    - chdir 到 sandbox 内的 cwd。

    Args:
        command: 原始 shell 命令。
        workspace: workspace 路径。
        cwd: 当前工作目录。

    Returns:
        包装后的 bwrap 命令字符串。
    """
    ws = Path(workspace).resolve()

    # 计算 sandbox 内的 cwd
    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    # 必须绑定的系统目录
    required = ["/usr"]
    # 可选绑定的系统目录（不存在时忽略）
    optional = [
        "/bin",
        "/lib",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/pki/tls/certs",
        "/etc/pki/ca-trust",
        "/etc/crypto-policies",
        "/etc/resolv.conf",
        "/etc/ld.so.cache",
    ]

    args = ["bwrap", "--new-session", "--die-with-parent", "--setenv", "HOME", str(ws)]

    for p in required:
        args += ["--ro-bind", p, p]
    for p in optional:
        args += ["--ro-bind-try", p, p]

    args += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),  # 隐藏 workspace 父目录（含 config.json）
        "--dir", str(ws),            # 重建 workspace 挂载点
        "--bind", str(ws), str(ws),  # workspace 读写绑定
        "--chdir", sandbox_cwd,
        "--", "sh", "-c", command,
    ]

    return shlex.join(args)


# 后端注册字典
_BACKENDS: dict[str, callable] = {
    "none": _none,
    "bwrap": _bwrap,
}


def wrap_command(sandbox: str, command: str, workspace: str, cwd: str = "") -> str:
    """根据沙箱后端名称包装命令。

    Args:
        sandbox: 沙箱后端名称（"none"/"bwrap"）。
        command: 原始 shell 命令。
        workspace: workspace 路径。
        cwd: 当前工作目录。

    Returns:
        包装后的命令字符串。

    Raises:
        ValueError: 未知的沙箱后端名称。
    """
    # 空字符串或 "none" 表示无沙箱
    if not sandbox or sandbox == "none":
        return command

    backend = _BACKENDS.get(sandbox)
    if backend is None:
        available = ", ".join(sorted(_BACKENDS.keys()))
        raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {available}")

    return backend(command, workspace, cwd)


def available_backends() -> list[str]:
    """列出所有可用的沙箱后端名称。

    Returns:
        后端名称列表。
    """
    return sorted(_BACKENDS.keys())
