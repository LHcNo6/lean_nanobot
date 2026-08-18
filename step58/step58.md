# Step 58: runner 收尾对齐

## 解决了什么问题

step57 的 runner 缺少 nanobot 中的几个辅助方法和常量：
- 请求参数构建散落在 `_request_model` 中，未提取为 `_build_request_kwargs`
- 最终 assistant 消息直接 append，无去重/替换逻辑（`_append_final_message`）
- 模型错误时无持久化占位符（`_PERSISTED_MODEL_ERROR_PLACEHOLDER`）
- 多模态 content 合并逻辑缺失（`_merge_message_content`）
- 工具错误结果判断函数缺失（`is_tool_error_result`）

## 原理思路

### 新增常量
- `_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"`

### 新增辅助方法（runner.py）
- `_merge_message_content(left, right)`：合并字符串/多模态 content
- `_build_request_kwargs(spec, messages, tools)`：提取 provider 请求参数构建
- `_append_final_message(messages, content)`：追加最终消息，避免重复（最后一条 assistant 无 tool_calls 时替换 content）
- `_append_model_error_placeholder(messages)`：追加错误占位符（已有 assistant content 时不重复）

### 新增函数（tool.py）
- `is_tool_error_result(name, result)`：判断 ToolResult 是否为错误

### 重构
- `_request_model` 中 provider 调用改用 `_build_request_kwargs`
- runner 结束时直接 append 改用 `_append_final_message`

## 核心函数/类

- `runner.py:_PERSISTED_MODEL_ERROR_PLACEHOLDER` - 模型错误占位符常量
- `runner.py:AgentRunner._merge_message_content` - content 合并
- `runner.py:AgentRunner._build_request_kwargs` - 请求参数构建
- `runner.py:AgentRunner._append_final_message` - 最终消息追加（去重）
- `runner.py:AgentRunner._append_model_error_placeholder` - 错误占位符追加
- `tool.py:is_tool_error_result` - 工具错误结果判断

## 测试结果

- 568 tests，3 个已知环境失败（非回归）
- 新增 18 个测试：
  - TestStep58MergeMessageContent（5 个）：字符串合并、空值、多模态 list
  - TestStep58AppendFinalMessage（6 个）：追加、去重、替换、tool_calls 后追加
  - TestStep58ModelErrorPlaceholder（3 个）：占位符追加、去重、常量文本
  - TestStep58IsToolErrorResult（3 个）：错误/成功/非 ToolResult 判断
  - TestStep58BuildRequestKwargs（1 个）：必要字段存在

## 暴露的问题

- `_append_model_error_placeholder` 已定义但尚未在 runner 错误路径中调用（模型错误时的持久化逻辑留待后续）
- `_merge_message_content` 已定义但尚未在消息合并路径中使用（多模态 content 合并留待后续）
- `is_tool_error_result` 已定义但 runner 中工具错误判断仍用内联逻辑（后续可替换）

## 下一 step

step59：loop 收尾对齐（_process_system_message skip/extend_to_user、workspace_scope.for_turn、runtime_events 参数、_dispatch CLI 空响应、_request_context_for_turn 重命名）。
