# Step 58: runner 收尾对齐

## 目标

对齐 nanobot runner 的辅助方法和常量：
1. `_PERSISTED_MODEL_ERROR_PLACEHOLDER` 常量
2. `is_tool_error_result` 函数（tool.py）
3. `_merge_message_content` 静态方法
4. `_build_request_kwargs` 方法（提取请求参数构建）
5. `_append_final_message` / `_append_model_error_placeholder` 静态方法

## 最小增量方案

### runner.py
- 添加 `_PERSISTED_MODEL_ERROR_PLACEHOLDER` 常量
- 添加 `_merge_message_content(left, right)` 静态方法
- 添加 `_build_request_kwargs(spec, messages, tools)` 方法，_request_model 中使用
- 添加 `_append_final_message(messages, content)` 静态方法，替换直接 append
- 添加 `_append_model_error_placeholder(messages)` 静态方法

### tool.py
- 添加 `is_tool_error_result(name, result)` 函数
