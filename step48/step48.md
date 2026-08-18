# Step 48 — `hook.finalize_content` + reasoning 提取

> 对齐 nanobot runner：主循环中提取 reasoning、调用 `hook.finalize_content`、通过 `emit_reasoning` 输出推理流。
> 上游：step47（_request_finalization_retry）。
> 下游：step49（usage 估算升级）。

## 一、本 step 做了什么

### 1.1 核心改动（3 个改动点，~25 行）

1. **导入 `extract_reasoning`**：从 `step48.helpers` 导入
2. **主循环中 reasoning 提取 + finalize_content**：
   - 调用 `extract_reasoning` 分离 `reasoning_content`/`thinking_blocks` 和 `content`
   - 设置 `response.content = cleaned_content`
   - reasoning 非空时调用 `emit_reasoning` + `emit_reasoning_end`（一次性输出）
   - 用 `hook.finalize_content(iter_ctx, response.content)` 替代直接用 `response.content`
3. **`_try_finalize_after_max_iterations` 中调用 finalize_content**：
   - `clean = hook.finalize_content(iter_ctx, response.content) or ""`

### 1.2 已有基础设施（无需新增）

- ✅ `extract_reasoning` 函数（helpers.py，step 早期已添加）
- ✅ `hook.finalize_content(context, content)` 方法（hook.py，step30 已添加）
- ✅ `hook.emit_reasoning(reasoning_content)` / `emit_reasoning_end()` 方法
- ✅ `context.streamed_reasoning: bool = False` 字段
- ✅ `LLMResponse` 的 `reasoning_content`、`thinking_blocks` 字段

## 二、关键实现细节

### 2.1 主循环中的处理顺序（与 nanobot 一致）

```python
# 1. 提取 reasoning
reasoning_text, cleaned_content = extract_reasoning(
    response.reasoning_content, response.thinking_blocks, response.content)
response.content = cleaned_content

# 2. 输出 reasoning 流（一次性，流式留到 step53）
if reasoning_text and not iter_ctx.streamed_reasoning:
    await hook.emit_reasoning(reasoning_text)
    await hook.emit_reasoning_end()
    iter_ctx.streamed_reasoning = True

# 3. 内容最终化
clean = hook.finalize_content(iter_ctx, response.content) or ""

# 4. 空响应检查
if self._is_blank_text(clean):
    # ...
```

### 2.2 `extract_reasoning` 优先级

1. `reasoning_content`（专用字段，DeepSeek-R1 / Kimi / OpenAI reasoning）
2. `thinking_blocks`（Anthropic）
3. 内联 `<think>` / `<thought>` 块

每个响应只贡献一个来源；高优先级存在时忽略低优先级，但内联 `<think>` 标签始终从 content 中剥离。

### 2.3 `finalize_content` 是同步方法

`hook.finalize_content` 是同步方法（不是 async），直接调用即可。CompositeHook 会链式调用所有 hook 的 finalize_content。

## 三、为什么是最小增量

| 做 | 不做（留到后续） |
|----|-----------------|
| 导入 `extract_reasoning` | 流式 reasoning（`stream_progress_deltas` + `IncrementalThinkExtractor`）— step53 |
| 主循环 reasoning 提取 | `_request_model` 中的 reasoning 流处理 — step53 |
| 主循环 `finalize_content` 调用 | progress streaming — step53 |
| `emit_reasoning` 一次性输出 | `on_thinking_delta` 增量推理流 — step53 |
| `_try_finalize_after_max_iterations` finalize_content | |

## 四、测试

新增 6 个测试：

| 测试 | 验证点 |
|------|--------|
| `test_extract_reasoning_strips_think_tags` | `<think>` 标签从 content 中剥离 |
| `test_extract_reasoning_from_reasoning_content` | reasoning_content 优先于内联 `<think>` |
| `test_extract_reasoning_no_reasoning` | 无推理内容时返回 (None, content) |
| `test_reasoning_content_extracted_and_emitted` | reasoning_content 被提取并通过 emit_reasoning 输出，不进入最终答案 |
| `test_think_tags_stripped_from_final_content` | 内联 `<think>` 标签从最终答案中剥离 |
| `test_finalize_content_called_in_main_loop` | 主循环中 hook.finalize_content 被调用 |

测试结果：464 tests，3 环境相关失败（与 step47 完全一致），零回归。

## 五、对齐度

| 维度 | step47 | step48 后 |
|------|--------|----------|
| `extract_reasoning` 调用 | ❌ | ✅ |
| `hook.finalize_content` 主循环调用 | ❌ | ✅ |
| `emit_reasoning` 调用 | ❌ | ✅（一次性） |
| `_try_finalize_after_max_iterations` finalize_content | ❌ | ✅ |
| 流式 reasoning | ❌ | ❌（step53） |
| progress streaming | ❌ | ❌（step53） |
| `on_thinking_delta` 增量流 | ❌ | ❌（step53） |

runner 对齐度：~70% → ~73%（A7/A31 部分完成）。
agent 综合对齐度：~87% → ~88%。

## 六、下一 step 衔接

- **step49**：usage 估算升级——不依赖 step48；
- **step53**：progress streaming + thinking 流——依赖 step48（reasoning 基础设施），实现增量推理流和 progress 增量；
- **step50**：`_run_tool` hook 生命周期——不依赖 step48。

step48 完成后，推理内容不再泄漏到最终答案，hook 的内容净化管线生效。
