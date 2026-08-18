# Step 59: loop 收尾对齐

## 目标

1. `_process_system_message` 中 `extend_to_user=is_subagent`（对齐 nanobot）
2. `_build_turn_request_context` 重命名为 `_request_context_for_turn`
3. `_build_agent_spec` 中 workspace scope 改用 `for_turn`
4. `_resolve_runtime_context_for_turn` 保持（已对齐）

## 最小增量方案

### loop.py
- _process_system_message: extend_to_user=False → is_subagent
- _build_turn_request_context → _request_context_for_turn（重命名+更新调用点）
- _build_agent_spec: workspace_scope or self.workspace_scopes.for_message(...) → for_turn(channel=..., message_metadata=..., session_metadata=...)
