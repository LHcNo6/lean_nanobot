# Step 22 — Providers Registry & Factory + Fallback（异常式）

在 Step 21 (CommandRouter & COMMAND 状态) 基础上，对齐 nanobot 的 providers
子系统：`providers/registry.py`（ProviderSpec 注册表 + find_by_name/find_by_model）、
`providers/factory.py`（ProviderSettings + make_provider + ProviderSnapshot）、
`providers/fallback_provider.py`（异常捕获式逐级回退 + 熔断）。同时把静态
`Runtime` 升级为不可变 `LLMRuntime` 骨架 + `ModelPreset`，`main.py` 的
`from_env()` 单例被工厂装配取代。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 21 时 provider 只有一条路：`OpenAICompatProvider.from_env()` 读三个
环境变量（OPENAI_API_KEY / BASE / MODEL），全部组件共享这一个实例。问题有三：

- **结构不对齐**：nanobot 有 `providers/registry.py`（ProviderSpec 元数据表）、
  `factory.py`（按配置/模型名装配）、`fallback_provider.py`（主模型挂了逐级
  回退到备用模型），我们连「回退」这个概念都没有——主模型 500/超时时整个
  turn 直接失败；
- **硬编码装配**：`from_env()` 把「读环境变量」写死在 provider 实现里，换
  provider、加回退、做配置热刷新都无从谈起；
- **Runtime 方向反了**：`loop.py` 用 `replay_budget` 正向构造 `Runtime`，而
  budget 本身又是 main.py 手算的 `context_window - max_tokens - buffer`；
  nanobot 的 `LLMRuntime` 是不可变快照，budget 应**从 runtime 反推**。

因此 step22 把 provider 提升为一等装配层，并给出 `LLMRuntime` 骨架，
为 step25 的 config 系统铺路。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 注册表 | `providers/registry.py`：`ProviderSpec`（frozen dataclass）+ `PROVIDERS` 6 条目（custom/openrouter/openai/deepseek/dashscope/ollama）+ `find_by_name`/`find_by_model`/`create_dynamic_spec` |
| 工厂 | `providers/factory.py`：`ProviderSettings`（纯 dataclass，无 config 前的装配输入）+ `make_provider` + `ProviderSnapshot`/`provider_signature` |
| 异常式回退 | `providers/fallback_provider.py`：主 provider 抛异常时逐级尝试 fallback；已流式发出内容不回退；连续 3 次失败熔断 60s |
| LLMRuntime | `llm.py`：`LLMRuntime`（frozen）+ `GenerationSettings` + `ModelPreset`/`resolve_preset`；保留遗留 `Runtime` 兼容旧测试 |
| loop 反推 budget | `AgentLoop.__init__(runtime=...)`：`replay_budget = context_window - max_tokens - 128`；spec 从 runtime 取 model/temperature/max_tokens |
| main.py 瘦身 | 删除 `from_env()` 单例；`make_provider(settings)` 装配；`FALLBACK_MODELS` 环境变量演示回退链 |

## 三、核心函数 / 类说明

### `providers/registry.py`
- `ProviderSpec`：name / keywords（模型名关键词）/ env_key / backend / default_api_base /
  is_gateway / is_local / is_direct / detect_by_key_prefix。**注册表顺序即匹配优先级**
  （gateway 在前、本地最后）。
- `find_by_name(name)`：小写、`-`/空格转 `_` 后精确匹配配置字段名。
- `find_by_model(model)`：关键词子串匹配模型名（"gpt-4o"→openai、"qwen-*"→dashscope）。
- `create_dynamic_spec(name)`：未注册自定义 provider 的动态 spec（is_direct 语义）。

### `providers/factory.py`
- `ProviderSettings`：model / provider / api_key / api_base / temperature / max_tokens /
  context_window_tokens / fallbacks（嵌套列表）。
- `make_provider(settings, for_fallback=False)`：解析 spec → 校验凭据（is_direct/is_local
  免 key、custom 必须给 api_base、其余必须有 key）→ 构造 `OpenAICompatProvider`；
  有 fallbacks 时包 `FallbackProvider`（`for_fallback=True` 防递归包装）。
- `ProviderSnapshot` / `provider_signature`：不可变快照与签名（A1 热刷新预留，
  step25 由 config 驱动）。

### `providers/fallback_provider.py`
- `is_fallbackable_exception(exc)`：**异常式分类**（step21 的 LLMResponse 无结构化
  错误字段，用异常代替 nanobot 的 `finish_reason=="error"`）：timeout/连接类、
  408/409/429/5xx 回退；400/401/403/404/422 与未知异常直接抛。
- `FallbackProvider`：请求级 failover。主 provider 先走自身 `chat_with_retry` 耗尽重试，
  仍抛异常才回退；fallback 由 `provider_factory` 按需创建；每次回退覆盖
  model/max_tokens/temperature 并还原 kwargs；已流式输出后失败 → 不回退直接抛；
  连续 3 次失败熔断 60s（半开探测）。
- 四个入口 `chat` / `chat_stream` / `chat_with_retry` / `chat_stream_with_retry`
  共享 `_try_chain`（runner 与 consolidation 都直接调用 provider，故四个都要）。

### `llm.py`
- `LLMRuntime`（frozen, slots）：provider / model / generation / context_window_tokens /
  model_preset / snapshot_signature + `capture()`；`max_tokens`/`temperature` 属性兼容
  遗留 `Runtime` API（consolidation 直接读）。
- `ModelPreset` + `resolve_preset()`：命名预设骨架（A1，step25 由 config 替代）。
- 遗留 `Runtime` 保留不动，旧测试零改动。

### `loop.py` / `main.py`
- `AgentLoop` 新增可选 `runtime` 参数：给 runtime 则反推 budget；给 `replay_budget`
  则构造 LLMRuntime；两者都无则报错。`AgentRunSpec` 从 runtime 取
  model/temperature/max_tokens/context_window_tokens（A1 方向：runtime 驱动 spec）。
- `main.py`：`_PRESETS` + `resolve_preset("default")` → `make_provider(settings)` →
  `LLMRuntime.capture(...)` → 注入 loop；`FALLBACK_MODELS`（逗号分隔）演示回退。

## 三、暴露的问题 / 偏离与取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| 异常式 vs 响应式回退 | nanobot 靠 `finish_reason=="error"` + error_kind 字段；我们 LLMResponse 无错误字段，用异常分类替代 | step30（H5 重试引擎）再迁移结构化错误字段 |
| 免 key 占位符 | openai SDK 构造要求非空 key，本地/直连 provider 用 `"missing"` 兜底（factory 已保证非 exempt 必填） | step25 配置系统后由 loader 统一管理 |
| 无动态配置探测 | `detect_by_key_prefix` 等字段已预留但工厂未实现自动探测（无 config 系统） | step25 |
| 熔断参数硬编码 | 3 次/60s 与 nanobot 相同，但不可配置 | step25 移入 config |
| ModelRuntimeResolver 未做 | 只落地 LLMRuntime 骨架 + presets；refresh/resolve_override 依赖 config 与热刷新 | step25 后补 |
| 流中 timeout 恢复（on_stream_recover） | nanobot 支持已流式 timeout 后开新段续传，我们直接抛 | step26 事件层时评估 |

## 四、下一步要解决什么

Step 23 — Mid-turn Injection 打通 + Subagent 系统消息通道（A2 + A3 + A6）：
修复「注入死代码」——subagent 回包应在 turn 内注入而非排队成独立 turn；
`_state_run` 的 `injection_callback` 接到 pending_queue、`channel=="system"`
分支按 `subagent_task_id` 去重；`TurnContext` 补 turn_id / runtime /
on_progress / on_stream / pending_queue 字段。
