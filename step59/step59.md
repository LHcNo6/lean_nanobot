# Step 59: loop 收尾对齐

## 解决了什么问题

step58 的 loop 与 nanobot 存在几处命名和逻辑差异：
- `_process_system_message` 中 `extend_to_user` 硬编码为 `False`，nanobot 中为 `is_subagent`
- 方法名 `_build_turn_request_context` 与 nanobot 的 `_request_context_for_turn` 不一致
- `_build_agent_spec` 中 workspace scope 使用 `for_message`，nanobot 使用 `for_turn`

## 原理思路

### 1. extend_to_user=is_subagent
subagent follow-up 消息作为 assistant 角色注入历史时，需要 `extend_to_user=True` 确保历史截断时保留到最近的 user 消息，避免 subagent 结果丢失上下文。

### 2. 方法重命名
`_build_turn_request_context` → `_request_context_for_turn`，与 nanobot 命名对齐。

### 3. workspace_scope.for_turn
`for_turn(channel, message_metadata, session_metadata)` 支持 scoped channel（如飞书）按 metadata 解析 workspace，比 `for_message(msg, session_metadata)` 更灵活。

## 核心函数/类

- `loop.py:AgentLoop._request_context_for_turn` - 重命名（原 _build_turn_request_context）
- `loop.py:AgentLoop._process_system_message` - extend_to_user=is_subagent
- `loop.py:AgentLoop._build_agent_spec` - workspace scope 改用 for_turn

## 测试结果

- 573 tests，3 个已知环境失败（非回归）
- 新增 5 个测试：
  - TestStep59RequestContextForTurn（2 个）：方法重命名验证
  - TestStep59WorkspaceScopeForTurn（2 个）：for_turn 方法存在、非 scoped channel 返回默认
  - TestStep59SystemMessageExtendToUser（1 个）：源码中 extend_to_user=is_subagent

## 下一 step

step60：配置层扩展 + from_config 完整对齐（channels_config/tools_config/web_config/exec_config、provider_snapshot_loader/preset_snapshot_loader、workspace: Path 参数）。
