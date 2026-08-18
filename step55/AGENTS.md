# Step 55: ModelRuntimeResolver 完整实现

## 目标

引入 ModelRuntimeResolver，将 runtime 选择/切换逻辑从 AgentLoop 中提取为独立服务：
1. `runtime` 属性委托给 resolver
2. `llm_runtime` 属性（refresh 入口，step55 暂不实现热刷新）
3. `set_model_preset` / `set_runtime_model` / `set_runtime_context_window` 方法
4. `model_preset` / `provider_signature` 属性

## 最小增量方案

### 新增 model_runtime.py
- `ModelRuntimeResolver` 类：持有不可变 LLMRuntime
- `runtime` property：返回当前 runtime
- `model_preset` / `provider_signature` property
- `current(refresh=False)`：返回当前 runtime（refresh 暂为 no-op）
- `select_model(model)`：用 dataclasses.replace 切换 model
- `select_preset(name)`：设置 model_preset（简化版，暂无预设表）
- `select_context_window(n)`：切换 context_window_tokens

### 修改 loop.py
- `__init__` 创建 `self._runtime_resolver`
- `runtime` 改为 property，委托 resolver
- 新增 `llm_runtime` property（调用 current）
- 新增 `set_model_preset` / `set_runtime_model` / `set_runtime_context_window`
- 新增 `model_preset` / `provider_signature` property

## 不做
- 不实现 ProviderSnapshot / build_provider_snapshot（留待 step60 配置层）
- 不实现 model_presets 预设表（无配置来源）
- 不实现 refresh 热刷新（无 provider_snapshot_loader）
- 不实现 _publish_runtime_selection（无 runtime_events model 变更通知）
