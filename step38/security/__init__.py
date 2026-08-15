"""Security package for lean_nanobot (step29).

对齐 nanobot `security/` 的最小子集（A10 + H7）：
- ``workspace_access.py``：Workspace 权限范围解析 + ContextVar 绑定（工具查询入口）；
- ``workspace_policy.py``：应用级路径边界守卫（不替代 OS sandbox）；
- ``network.py``：最小 SSRF / loopback 校验（无 httpx 传输层）。
"""
