# Step 22 — Data Flow (Providers Registry & Factory + Fallback)

step22 新增/变更的链条：① 装配链（环境变量 → 注册表 → 工厂 → LLMRuntime 冻结）② budget 反推链（方向修正）③ 回合内 provider 调用链 ④ 回退+熔断链。

---

## 图 0：全局视图（四条链）

```
   ┌──────────────────────────── 回合编排链 (step21 原样，8 态) ────────────────────────────┐
   │  RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE                                    │
   │                                                                                          │
 ①装配链(main.py)                   ③回合内调用链                      ④回退+熔断链           ②budget反推
 环境变量: OPENAI_MODEL/            _state_run 组 spec          FallbackProvider._try_chain   LLMRuntime
  OPENAI_PROVIDER/API_KEY/API_BASE  model/temp/max_tokens       ├─primary 先重试               frozen快照
  FALLBACK_MODELS                     ← runtime 4字段(312-315)  ├─is_fallbackable_exception   → loop.replay_budget
   │                                 → runner._request_model    ├─fallback[0..n]              = cw - max - 128
  resolve_preset(_PRESETS)          (runner.py:164-167)         ├─已流式→不回退               → spec 4字段
   │                                 → spec.provider            └─3次→熔断60s→半开            → consolidation/autocompact
  _build_settings → ProviderSettings  .chat_stream_with_retry                                  （runtime 驱动一切）
   │                                 → OpenAICompatProvider
  make_provider(settings)            → AsyncOpenAI SDK
   │  ├─注册表定位(find_by_name/     → HTTP → 远端模型
   │  │   find_by_model → PROVIDERS)
   │  ├─凭据校验(_resolve_credentials)
   │  └─fallbacks? → 包 FallbackProvider
   ▼
  LLMRuntime.capture → 注入 AgentLoop(runtime=)
```

---

## 图 1：装配链（★全新，main.py:27-85 → factory.py → registry.py）

```
main.py 装配（组合根，step21 的 from_env() 单例被删除）
   _PRESETS = {"default": ModelPreset(...)}               (main.py:27-35)
      model=OPENAI_MODEL(默认 gpt-4o-mini)  provider=OPENAI_PROVIDER
      context_window_tokens=1024  max_tokens=128  temperature=0.7
   preset = resolve_preset(_PRESETS, "default")           (main.py:76, llm.py:97-109)
      │ 未命中 → KeyError（含可用列表提示）
   settings = _build_settings(preset)                     (main.py:38-61)
      │  api_key=OPENAI_API_KEY, api_base=OPENAI_API_BASE
      │  fallbacks = [ProviderSettings(m) for m in FALLBACK_MODELS.split(",")]
      ▼
   provider = make_provider(settings)                     (main.py:77, factory.py:116-128)
      ├─ _build_provider(settings)                        (factory.py:96-113)
      │    ├─ _resolve_spec: 显式 provider 名 → 动态 custom  ← 模型关键词 find_by_model
      │    │                  (factory.py:54-63, registry.py:107-142)
      │    ├─ backend != "openai_compat" → ValueError（预留）
      │    ├─ _resolve_credentials(settings)              (factory.py:66-93)
      │    │    ├─ api_key/api_base 缺省时从 spec.env_key / spec.default_api_base 兜底
      │    │    ├─ is_local/is_direct（免 key）→ 必须有 api_base，key 可为 ""
      │    │    └─ 其余 → 无 key 抛 ValueError("No API key configured...")
      │    └─ OpenAICompatProvider(api_key, api_base, model)   (openai_compat_provider.py:16-33)
      │         api_key or "missing"                      ★SDK 要求非空 key 的占位符(25)
      └─ settings.fallbacks 且非 for_fallback:
            → FallbackProvider(primary, fallback_presets,
                               provider_factory=lambda fb: make_provider(fb, for_fallback=True))
              ★递归构造但禁用自身回退，防递归包装       (factory.py:122-127)
   runtime = LLMRuntime.capture(provider, model,           (main.py:78-85, llm.py:52-76)
                                context_window_tokens, max_tokens, temperature,
                                model_preset="default")
      ▼ ★不可变快照冻结（frozen+slots，llm.py:26-76），进入 turn 前全部参数定格
   AgentLoop(..., runtime=runtime)                        (main.py:111-122)
   同一个 provider 实例同时注入:
      ├─ SubagentManager(provider=provider)   ★子代理也走同一回退链
      └─ runner spec.provider（图 3）
```

---

## 图 2：budget 反推链（★方向修正，loop.py:126-144）

```
AgentLoop.__init__(replay_budget=None, runtime,None)    (loop.py:94-109)
   ├─ runtime 给了 → self.runtime = runtime（外部装配优先）
   ├─ 只给 replay_budget → 兼容旧路径: LLMRuntime.capture(context_window=max(budget,0))
   └─ 两个都没有 → ValueError("AgentLoop requires replay_budget or runtime")
   ▼
if replay_budget 给了 → 原样使用
else → 反推: replay_budget = context_window_tokens - generation.max_tokens - 128
        (loop.py:137-144, _REPLAY_SAFETY_BUFFER=128 在 loop.py:39)
   ★step21: main.py 手算 budget 传给 loop → step22: 方向反过来，budget 是 runtime 的派生值
   ▼
LLMRuntime 的消费方（runtime 驱动一切）:
   ├─ loop.replay_budget      ← 137-144                    → _state_build get_history (loop.py:265)
   ├─ AgentRunSpec 4 字段     ← 图 3 (loop.py:312-315)     → runner / governance
   ├─ Consolidator.archive    ← consolidation.py:159-160    → runtime.model + max_tokens=1024
   │    (runtime.provider.chat, consolidation.py:154)       ★consolidation 也走 provider，同样吃回退
   └─ AutoCompact._archive    ← autocompact.py:93-100
        (resolve_runtime=lambda: self.runtime, loop.py:170)
```

---

## 图 3：回合内 provider 调用链（_state_run → runner → provider）

```
_state_run(ctx)                                            (loop.py:281-322)
   │  ToolLoader 装载 → StreamPublishingHook → hook 组装（step20/21 原样）
   │  spec = AgentRunSpec(
   │      provider=self.provider,                        ← 可能是 FallbackProvider 包装
   │      model=self.runtime.model,                      ★step22 新增 4 字段 (312-315)
   │      temperature=self.runtime.generation.temperature,
   │      max_tokens=self.runtime.generation.max_tokens,
   │      context_window_tokens=self.runtime.context_window_tokens,
   │      ...)
   ▼
AgentRunner.run(spec)                                      (runner.py:83-115)
   ├─ _resolve_gov_config: context_window_tokens / max_tokens → governance 预算 (117-125)
   ├─ _run_loop: 迭代回合（run_goal_continuation/injection/工具批量执行，step21 原样）
   └─ _request_model(spec, ...)                            (runner.py:148-175)
        timeout = spec.llm_timeout_s or 300（流式放宽 2×）
        coro = spec.provider.chat_stream_with_retry(
                   messages, tools,
                   model=spec.model, temperature=spec.temperature,
                   max_tokens=spec.max_tokens, on_content_delta=...)   (164-167)
        await asyncio.wait_for(coro, outer_timeout)        (170)
        ▼ TimeoutError → LLMResponse(finish_reason="error")（turn 不崩）
   ▼
spec.provider 二选一:
   ├─ 无 fallbacks → Prompt）；|| AsyncOpenAI SDK
   │    → AsyncOpenAI().chat.completions.create            ★网络边界
   └─ 有 fallbacks → FallbackProvider.chat_stream_with_retry → 图 4
   ▼
返回 LLMResponse → 流式 delta 经 hook.on_stream → StreamDeltaEvent → bus（step20 链原样）
```

---

## 图 5：回退链（★全新，fallback_provider.py:219-296 `_try_chain`）

```
FallbackProvider.chat_stream_with_retry(..., on_content_delta)     (158-190)
   │  _StreamGuard() 包装 delta: 首个非空 delta → guard.delta_delivered=True
   ▼
_try_chain(method, kwargs, guard)                             (219-296)
   ├─ _primary_available()?                                 (196-200)
   │    ├─ 熔断中（冷却期内）→ 跳过 primary，直接 fallback（248-249）
   │    └─ 可用 → primary_attempted=True
   │         try: primary 自身先走 chat_with_retry 耗尽重试
   │         ├─ 成功 → _record_primary_success() → 返回
   │         ├─ cancelled → 上抛
   │         └─ 异常:
   │               ├─ guard.delta_delivered=True → ★已流式发出内容，不回退直接抛(238-242)
   │               │     （fallback 会重新从头输出，造成重复；宁可失败）
   │               ├─ not is_fallbackable_exception → 直接抛（认证/参数类，回退无用）
   │               └─ 可回退 → last_exc 记录 + _record_primary_failure() (246)
   │                   连续 3 次 → 熔断（图 6）
   ▼
   for preset in fallback_presets:                        (251-289)
   │    fallback_provider = self._provider_factory(preset)  ★按需创建(253)
   │    │    工作失败 → skip（该 fallback 跳过）
   │    original = {model, max_tokens, temperature} 快照   (258-261)
   │    kwargs[model/max_tokens/temperature] = preset 值   ★覆盖主模型参数(262-264)
   │    try: await fallback_provider.method(**kwargs)
   │    │    ├─ ✓ 成功 → log + 返回
   │    │    ├─ 已流式/不可回退 → raise
   │    │    └─ 可回退 → 记录 last_team 继续下一个
   │    finally: 还原 kwargs 三个字段                      ★请求参数不被污染(284-289)
   ▼
   全部失败:
   ├─ primary 尝试过 → re-raise last_error（primary 错误优先）(293-295)
   └─ primary 被熔断跳过 → RuntimeError("circuit open and all fallbacks failed") (294-295)
   ★四入口 chat/chat_stream/chat_with_retry/chat_stream_with_retry 共享 _try_chain
     （runner 与 consolidation 都直接调 provider，故四入口都要有）
```

---

## 图 7：熔断状态机（fallback_provider.py:196-213）

```
   ┌─────────────────┐  连续 3 次可回退失败(202-209)   ┌─────────────────┐
   │  CLOSED（正常） │ ─────────────────────────────► │  OPEN（熔断）    │
   │  _failures=0   │                                 │  _primary_id=    │
   │  primary 参与   │ ◄────────────────────────-----  │  primary 越过    │
   │                │  成功或冷却 60s（半开)          │  直接走 fallback │
   └─────────────────┘                                 └─────────────────┘
   半开探测: 冷却期后 _primary_available()=True → 放行一次请求
   ├─ 成功 → _record_primary_success(): 下计数、解除熔断(211-213)
   └─ 失败 → 再次熔断（从 3 次起算重新要求主）
   ★测试锚点: TestFallbackProvider.test_circuit_breaker_trips_after_three (test.py:5221-5236)
```

---

## 图 8：异常分类决策树（is_fallbackable_exception, fallback_provider.py:37-60）

```
异常 ──► asyncio.TimeoutError ────────────────► 回退 ✓
    ├─► openai.APIConnectionError / APITimeoutError ──► 回退 ✓
    ├─► openai.APIStatusError → status_code:
    │    ├─ 408/409/429 → 回退 ✓      (速率/冲突/并发，transient)
    │    ├─ 500-599     → 回退 ✓      (服务器侧，transient)
    │    └─ 其他        → 不回退 ✗
    ├─► 普通异常带 status_code 属性（如测试 _StatusError）: 同上判定 (55-59)
    └─► 未知异常 → 不回退 ✗  (避免掩盖编程错误)
   ★400/401/403/404/422 硬编码为不可回退 (34): 认证/参数错误回退到任何模型都一样
   ★测试锚点: TestFallbackClassification (test.py:5099-5115)
```

---

## 图 9：关键数据结构 · （★ = 新增/变更）

```
ProviderSettings（dataclass）                  (factory.py:29-40)
   model / provider / api_key / api_base / temperature / max_tokens /
   context_window_tokens / fallbacks: list[ProviderSettings]（可嵌套）
   ▼ make_provider
OpenAICompatProvider 或  FallbackProvider       (factory.py:116-128)
   FallbackProvider.model 转发 primary.model     (fallback_provider.py:83-85)
   ▼
ProviderSection(frozen)                       (factory.py:43-51, 160-171)
   provider / model / context_window_tokens / signature / generation
   signature = provider_signature(settings)    (131-144) ★热刷新检测用
   （step22 无消费方，step25 config 启用）
   ▼  LLMRuntime.capture
LLMRuntime（frozen+slots）                    (llm.py:26-76)
   provider / model / generation(GenerationSettings) / context_window_tokens /
   model_preset / snapshot_signature
   @property max_tokens/temperature → 桥接（用 run-time API 33-50）
   ★遗留 Runtime(可变) 保留 (llm.py:7-14) → 旧测试零改动
   ▼ 消费（图 2）
loop.replay_budget / spec 4 字段 / consolidation / autocompact
   ▼ 出站（不变）
OutboundMessage / StreamDeltaEvent → bus → manager._dispatch_outbound → channel
```

---

## 图 8：阅读路径建议（对比 step20/step21）

```
图 1 (装配链)       ← 环境变量怎么变成 provider 实例；missing 占位符的来龙去脉
  │
  ▼
图 2 (budget 反推)   ← 为什么方向掉转一会儿；runtime 冻结位置 = 数据契约
  │
  ▼ 回合真正调用时
图 3 (调用链)       ← spec 4 字段 ← runtime；TimeoutError → finish_reason="error" 不崩 turn
  │
  ▼ 主模型挂了
图 4 (回退链)       ← 已流式不回退 / 参数覆盖与还原 / for_fallback 防递归
  │
  ▼ 连续失败
图 5 (熔断状态机)   ← 3 次/60s/半开探测
  │
  ▼ 何时回退
图 6 (异常分类)     ← transient vs 认证/参数错误的二分
  ▼
图 7 (数据结构)     ← ProviderSettings → FallbackPacked → LLMRuntime 三层形状对照

核心一句话：step22 把所有“模型的元决策”前移到装配期并冻结进不可变 LLMRuntime，
运行期只干两件事——spec 从 runtime 取参数、provider 自带回退与熔断。
回合状态机、通道、命令（step19-21 的成果）全部零改动。
```