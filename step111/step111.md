# Step111：Subagent 工具集隔离（scope="subagent"）

> 规范文档：`proposal.md`（需求）/ `design.md`(架构) / `api-spec.md`（接口契约）
> 参考源码：`nanobot/nanobot/agent/subagent.py:197-217 _build_tools`

## 一、问题背景

对齐分析发现：step110 的 `SubagentManager` 通过构造参数直接持有**主 agent 的
ToolRegistry**（main.py:103 `tools=registry`），子代理因此能看到并调用全部核心工具：

- **递归 spawn**：子代理可再调 `spawn` 生成子代理，任务树可能失控，仅有全局
  并发计数兜底，无结构性防护；
- **越权调用**：`message`（主动向用户发消息）、`create_goal`/`update_goal`
  （改写会话目标）、`self`/`my`（内省主循环）、`cron_*`、`mcp_*` 等
  主 agent 专属能力全部暴露给子代理。

nanobot 的解法是 scope 过滤：SubagentManager 自建注册表，只装载声明了
`"subagent"` scope 的工具——spawn/message/goal 等因 scope 不含 subagent
而**在 LLM 工具 schema 层面就不存在**。

## 二、本 step 解决什么、为什么这样做

**解决**：主/子代理的工具权限边界缺失。

**为什么选 scope 过滤**：
1. 对齐 nanobot 同款机制，对齐有据；
2. 成本极低——`ToolLoader.load(ctx, registry, scope=...)`（loader.py:55-60）
   早已支持按 `_scopes` 过滤，且本代码库 11 个工具已预先声明了含
   `"subagent"` 的 scope，本 step 只是"接通最后一段线"；
3. 工具不可见优于运行时报错：不进 schema 列表省 token，LLM 也无从误选。

**为什么每次 spawn 构建 registry**（而非 init 构建一次缓存）：
与 nanobot `_run_subagent` 中 per-spawn 调用 `_build_tools` 的结构保持一致，
为后续 step 按 workspace_scope 差异化构建留出空间；构建本身是纯对象实例化，
成本可忽略。

## 三、原理思路与具体实现

### 3.1 装配链路

```
SubagentManager._build_tools()
    ├─ ToolContext(config=扁平视图, workspace, restrict_to_workspace,
    │             exec_session_manager=manager共享实例,
    │             file_state_store=全新 FileStateStore)
    │   ※ 刻意不注入 bus / subagent_manager / sessions —— 双保险
    └─ ToolLoader().load(tool_ctx, registry, scope="subagent")
        → 只装载 _scopes ⊇ {"subagent"} 的工具类
```

### 3.2 关键实现点

1. **接口变更**：移除 `tools` 参数（破坏性），新增 `config`/`workspace`/
   `restrict_to_workspace`（keyword-only）；`_run_subagent` 改用
   `tools=self._build_tools()`。
2. **配置扁平化适配 `_flatten_tools_config`**（调查中发现的隐藏约束）：
   工具的 `enabled()/create()` 按根级 `.web` / `.exec` / `.tools` 读取配置
   （测试惯用 SimpleNamespace 形态），而真实 pydantic Config 只有根级
   `.tools`——直接传入会使 web/exec 组在 loader 的静默异常处理下悄悄缺席。
   适配器统一三种输入形态：完整 Config → 扁平化；已扁平 duck-view →
   字段级透传（重建自有包装，restrict 覆写不污染调用方对象）；None →
   默认 ToolsConfig。
3. **状态共享语义**（对齐 nanobot）：`ExecSessionManager` 由 manager 持有、
   跨子代理共享；`FileStateStore` 每次 `_build_tools()` 全新实例，并发子代理
   的 read-dedup / read-before-edit 状态互不污染。
4. **双保险防递归**：scope 白名单为主；ToolContext 缺少 spawn 类工具的创建
   依赖为辅（即使未来误标 scope，create() 失败也会被 loader 跳过）。
5. **main.py 装配**：改传 `config=config, workspace=workspace`，
   主 registry 装配顺序不变。

### 3.3 工具集结果

子代理现可见 **11 个工具**：`exec`、`read_file`、`write_file`、`edit_file`、
`list_dir`、`find_files`、`grep`、`web_search`、`web_fetch`、`write_stdin`、
`apply_patch`。nanobot 为 13 个（多 `cli_apps`、`list_exec_sessions`，
learn_nano 尚未实现这两个工具类）。

## 四、核心函数/类说明

| 符号 | 位置 | 功能 |
|------|------|------|
| `SubagentManager.__init__` | subagent.py | 新签名装配；解析 restrict_to_workspace（显式实参 > config 值）并覆写视图 |
| `SubagentManager._build_tools` | subagent.py:191 | 构建 subagent-scope 独立注册表（本 step 核心） |
| `SubagentManager._run_subagent` | subagent.py | per-spawn 调用 `_build_tools()` 替换原共享 registry |
| `_flatten_tools_config` | subagent.py:55 | 配置三形态统一适配，返回 (扁平视图, restrict 解析值) |
| `_copy_section_with` | subagent.py:35 | section 浅拷贝 + 字段覆写（pydantic/namespace 双兼容） |

## 五、测试与验证

新增 `tests/test_subagent_tool_isolation.py`（11 用例全 mock）：

- 白名单恰含 11 工具 / 黑名单零泄漏；
- 端到端防递归：mock provider 让子代理首轮请求 `spawn` → 第二轮请求中出现
  `"Tool 'spawn' not found"` 错误工具结果，且全程无嵌套任务产生；
- web/exec 组级开关生效；
- FileStates 跨构建隔离、ExecSessionManager 共享；
- 配置扁平化三形态 + restrict 显式覆盖优先。

回归结果（Windows 环境）：

| 套件 | step110 基线 | step111 | 结论 |
|------|-------------|---------|------|
| tests/ 新增隔离用例 | — | 11 passed | ✅ |
| tests/ 全量 | 1082 passed / 25 failed | 1093 passed / 25 failed（同一失败集）| ✅ 零回归 |
| test.py 全量 | 561 passed / 21 failed | 561 passed / 21 failed（同一失败集）| ✅ 零回归 |

失败集均为环境相关既有问题（unix 路径/bwrap 沙箱/CLI 终端交互等），
与本 step 无关（已在 step110 上复现确认）。

## 六、本 step 暴露的问题

1. **主 agent 工具装配存在同样的配置口径缺陷**：loop.py:1248-1257 把完整
   pydantic Config 直接作为 ToolContext.config 传入 core 加载，shell/web 组
   工具的 `enabled()` 会抛 AttributeError 被 loader 静默吞掉——生产路径下主
   agent 实际可能一直缺少 shell/web 工具。本 step 在子代理侧用扁平化适配器
   解决了，主循环侧未动（超出本 step 范围）。
2. **子代理上下文断裂依旧**：未绑定 RequestContext/session_key/workspace_scope，
   会话类工具即使放开也不可用，文件边界仅靠 allowed_dir 近似。
3. **loader 静默失败无观测**：任何 enabled/create 异常都被吞掉，工具缺席只能靠
   测试断言兜底，运行期不可感知。
4. **nanobot 的 `cli_apps`/`list_exec_sessions` 工具缺失**，子代理工具集比源码少 2 个。
5. `SubagentStatus.started_at` 仍是死字段；phase 无外部查询接口。

## 七、下一 Step 方向（候选，按最小增量）

1. **step112 候选**：子代理内绑定 RequestContext/workspace_scope（contextvars
   bind/reset，对齐 nanobot subagent.py:315-343），修复会话依赖工具与文件边界；
2. 主循环 core 装配复用 `_flatten_tools_config`，修复暴露问题 1；
3. spawn 增加 `temperature` 参数 + runtime 冻结派生；
4. LLM wall-clock 超时链路（goal 会话放宽 + NANOBOT_LLM_TIMEOUT_S 兜底）；
5. system prompt 模板文件化（skills/workspace 注入，对齐 subagent_system.md）。
