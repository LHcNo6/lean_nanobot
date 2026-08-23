# Step 91 Design: MyTool 嵌套属性访问

## 1. 架构

```
context.py（修改）
  └── ToolContext +agent_loop 字段

tools/self.py（修改）
  ├── _resolve_nested_path(obj, parts)  嵌套属性解析
  ├── MyTool._get_runtime_value         支持点分路径
  └── MyTool._has_key                   支持嵌套路径检测
```

## 2. 嵌套属性解析

```python
def _resolve_nested_path(obj, parts: list[str]) -> Any:
    """逐段解析嵌套属性路径。

    每段都检查：
    - _DENIED_ATTRS：Python 内部属性禁止访问
    - _BLOCKED：核心基础设施禁止访问
    - 不存在的属性返回 None
    """
    current = obj
    for part in parts:
        if part in _DENIED_ATTRS:
            raise PermissionError(f"Access denied: {part}")
        if part in _BLOCKED:
            raise PermissionError(f"Property '{part}' is blocked")
        if current is None:
            return None
        current = getattr(current, part, None)
    return current
```

## 3. key 解析规则

1. 如果 key 包含 `.`，按点分割为路径
2. 第一段是顶级 key（workspace/config/agent/exec_timeout 等）
3. 后续段是嵌套属性
4. `agent` 顶级 key 映射到 `ctx.agent_loop`

## 4. 安全边界

- 嵌套路径的每一段都检查 _DENIED_ATTRS 和 _BLOCKED
- 敏感字段过滤在最终结果的 _safe_repr 中生效
- set 操作仍然只支持单层白名单属性

## 5. 测试策略

- 单层属性向后兼容
- 嵌套属性 get（config.exec.timeout）
- agent key 映射到 agent_loop
- 嵌套路径中 BLOCKED 属性报错
- 嵌套路径中 Python 内部属性报错
- 不存在的嵌套属性返回 None
- 敏感字段在嵌套结果中被过滤
