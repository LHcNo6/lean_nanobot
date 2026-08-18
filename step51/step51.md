# Step 51 — SSRF/workspace 安全检测

> 对齐 nanobot runner：SSRF 阻断 + workspace 违规检测 + 重复外部查找阻断 + 重复违规升级。
> 上游：step50（_run_tool hook 生命周期 + 三元组返回）。
> 下游：step52（fail_on_tool_error + tool_events）。

## 一、本 step 做了什么

### 1.1 核心改动（5 个改动点，~200 行）

1. **helpers.py 新增重复检测函数（~80 行）**：
   - `external_lookup_signature` — web_fetch:url / web_search:query 签名
   - `repeated_external_lookup_error` — 阈值 2 次后阻断
   - `workspace_violation_signature` — 提取 path/file_path/target 签名
   - `_normalize_violation_target` — 规范化路径
   - `repeated_workspace_violation_error` — 阈值 2 次后升级提示
   - `_MAX_REPEAT_EXTERNAL_LOOKUPS = 2`、`_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2`

2. **runner.py 新增类变量和方法（~80 行）**：
   - `_SSRF_MARKERS`、`_SSRF_BOUNDARY_NOTE`、`_WORKSPACE_VIOLATION_MARKERS`
   - `_is_ssrf_violation` / `_is_workspace_violation`
   - `_classify_violation` — 统一分类：SSRF → 软 payload，workspace → 软 payload 或升级
   - `_ssrf_soft_payload` / `_event_detail`

3. **`_run_tool` 添加 counts 参数 + 4 个检测点（~30 行）**：
   - 开头：`repeated_external_lookup_error` — 重复外部查找阻断
   - prepare_call 出错：`_classify_violation`
   - 工具执行异常：`_classify_violation`
   - 工具返回错误结果（`ToolResult.is_error`）：`_classify_violation`

4. **`_execute_tool_batch` 添加 counts 参数并传递（~5 行）**

5. **主循环初始化 counts 并传递（~5 行）**：
   - `external_lookup_counts = {}`、`workspace_violation_counts = {}`
   - 整个 turn 中持久化，多 batch 共享

### 1.2 修复
- step50 测试中 `_run_tool`/`_execute_tool_batch` 调用添加 counts 参数

## 二、关键实现细节

### 2.1 `_classify_violation` 逻辑

```python
def _classify_violation(self, *, raw_text, soft_payload, event, tool_call, workspace_violation_counts):
    if self._is_ssrf_violation(raw_text):
        event["detail"] = "ssrf_violation: ..."
        return self._ssrf_soft_payload(raw_text), event, None
    if self._is_workspace_violation(raw_text):
        escalation = repeated_workspace_violation_error(...)
        if escalation:
            event["detail"] = "workspace_violation_escalated: ..."
            return escalation, event, None
        return soft_payload, event, None
    return None  # 非安全违规，继续正常处理
```

### 2.2 SSRF 是硬边界

SSRF 违规返回 `_SSRF_BOUNDARY_NOTE`，明确告知：
- 这是不可绕过的安全边界
- 不要用 curl/wget/编码 IP/替代 DNS/重定向/代理/其他工具重试
- 请用户提供本地文件、日志、截图或明确的安全公共 URL

### 2.3 workspace 是软边界 + 重复升级

- 前 2 次 workspace 违规：返回原错误（soft_payload）
- 第 3 次及以后：返回升级提示，明确告知"拒绝重复绕过尝试"

### 2.4 重复外部查找阻断

- web_fetch 同一 URL 或 web_search 同一 query，前 2 次正常
- 第 3 次及以后：返回"重复外部查找已阻断"，提示用已有结果或换来源

### 2.5 counts 生命周期

`external_lookup_counts` 和 `workspace_violation_counts` 在 `_run_loop` 中初始化，在整个 turn 中持久化。多个 batch、多次迭代共享同一个 counts 字典。

## 三、为什么是最小增量

| 做 | 不做（留到后续） |
|----|-----------------|
| helpers.py 新增重复检测函数 | 不修改 `AgentRunResult` 添加 tool_events（step52） |
| runner.py 新增 SSRF/workspace 检测方法 | 不添加 `fail_on_tool_error` 逻辑（step52） |
| `_run_tool` 添加 counts 参数 + 4 个检测点 | 不修改 `_execute_tool_batch` 返回值结构（已有三元组） |
| `_execute_tool_batch` 传递 counts | |
| 主循环初始化 counts | |

## 四、测试

新增 11 个测试：

| 测试 | 验证点 |
|------|--------|
| `test_is_ssrf_violation_detects_markers` | `_is_ssrf_violation` 检测 SSRF markers |
| `test_is_workspace_violation_detects_markers` | `_is_workspace_violation` 检测 workspace markers |
| `test_is_workspace_violation_includes_ssrf` | `_is_workspace_violation` 也包含 SSRF markers |
| `test_ssrf_soft_payload_includes_boundary_note` | `_ssrf_soft_payload` 包含边界说明 |
| `test_classify_ssrf_returns_soft_payload` | `_classify_violation` SSRF 命中返回软 payload |
| `test_classify_workspace_returns_soft_payload` | `_classify_violation` workspace 命中返回软 payload |
| `test_classify_non_violation_returns_none` | `_classify_violation` 非安全违规返回 None |
| `test_repeated_external_lookup_blocks_after_2` | 重复外部查找 2 次后阻断 |
| `test_repeated_workspace_violation_escalates_after_2` | 重复 workspace 违规 2 次后升级 |
| `test_run_tool_ssrf_violation_blocked` | `_run_tool` 中 SSRF 违规被阻断 |
| `test_run_tool_repeated_external_lookup_blocked` | `_run_tool` 中重复外部查找被阻断 |

测试结果：492 tests，3 环境相关失败（与 step50 完全一致），零回归。

## 五、对齐度

| 维度 | step50 | step51 后 |
|------|--------|----------|
| SSRF 检测 | ❌ | ✅ |
| workspace 违规检测 | ❌ | ✅ |
| 重复外部查找阻断 | ❌ | ✅ |
| 重复 workspace 违规升级 | ❌ | ✅ |
| `_classify_violation` 统一分类 | ❌ | ✅ |
| `fail_on_tool_error` | ❌ | ❌（step52） |
| tool_events 存入结果 | ❌ | ❌（step52） |

runner 对齐度：~78% → ~82%（A33 完成）。
agent 综合对齐度：~90% → ~91%。

## 六、下一 step 衔接

- **step52**：fail_on_tool_error + tool_events——依赖 step51（`_classify_violation` 已返回三元组，step52 添加 fatal_error 和 events 存入结果）；
- **step53**：progress streaming + thinking 流——不依赖 step51。

step51 完成后，SSRF/workspace 安全边界生效，重复外部查找和 workspace 违规被阻断/升级。
