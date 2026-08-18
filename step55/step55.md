# Step 55: ModelRuntimeResolver 完整实现

## 解决了什么问题

step54 中 `self.runtime` 是 AgentLoop 的直接属性，模型切换逻辑（set_model_preset/set_runtime_model）散落在 loop 中或缺失。nanobot 将 runtime 管理提取为独立的 `ModelRuntimeResolver`，使命令层、SDK、工具层可以依赖这个公共服务而不触碰 loop 私有状态。

## 原理思路

### ModelRuntimeResolver（新增 model_runtime.py）

- 持有不可变 `LLMRuntime`，所有切换通过 `dataclasses.replace` 创建新实例
- `runtime` property 返回当前值（不刷新）
- `current(refresh=False)` 是解析入口，step55 中 refresh 为 no-op（无 provider_snapshot_loader）
- `select_model(model)` 切换模型并清除 preset
- `select_preset(name)` 设置 model_preset 字段（step55 简化版，不重建 provider）
- `select_context_window(n)` 切换上下文窗口
- `model_preset` / `provider_signature` 只读 property

### AgentLoop 委托

- `__init__` 创建 `self._runtime_resolver = ModelRuntimeResolver(initial_runtime)`
- `runtime` 改为 property，委托 `resolver.runtime`
- 新增 `llm_runtime` property（调用 `current(refresh=True)`，step55 等价于 `runtime`）
- 新增 `set_model_preset` / `set_runtime_model` / `set_runtime_context_window` 方法
- 新增 `model_preset` / `provider_signature` property

### 最小增量边界

- 不实现 ProviderSnapshot / build_provider_snapshot（step60 配置层）
- 不实现 model_presets 预设表（无配置来源）
- 不实现 refresh 热刷新（无 provider_snapshot_loader）
- 不实现 _publish_runtime_selection（runtime_events model 变更通知留待后续）

## 核心函数/类

- `model_runtime.py:ModelRuntimeResolver`：runtime 选择与切换服务
- `loop.py:AgentLoop.runtime`（property）：委托 resolver
- `loop.py:AgentLoop.llm_runtime`（property）：turn 准入时的解析入口
- `loop.py:AgentLoop.set_model_preset(name)`：选择预设
- `loop.py:AgentLoop.set_runtime_model(model)`：切换模型
- `loop.py:AgentLoop.set_runtime_context_window(n)`：切换上下文窗口

## 测试结果

- 532 tests，3 个已知环境失败（非回归）
- 新增 18 个测试：
  - TestStep55ModelRuntimeResolver（13 个）：runtime/preset/signature/current/select_model/select_preset/select_context_window/参数校验/不可变性
  - TestStep55AgentLoopRuntime（5 个）：property 和方法存在性

## 暴露的问题

- select_preset 是简化版，不重建 provider；完整实现需要 model_presets 配置表和 preset_snapshot_loader，留待 step60。
- llm_runtime 的 refresh 路径未实现，配置热刷新需要 provider_snapshot_loader。

## 下一 step

step56：media 处理（_prepare_message_media / extract_documents / image_placeholder_text）。
