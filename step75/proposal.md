# Step 75 Proposal: MyTool 运行时自省

## 1. 问题背景

agent 无法查看和修改自身的运行时配置（如当前 workspace、超时设置、工具启用状态等）。
nanobot 的 MyTool 提供运行时状态自省和配置修改能力，有严格的安全边界。

## 2. 目标

新建 `tools/self.py`，实现简化版 MyTool：
1. `get` 操作：查看运行时属性（workspace、config、工具列表等）
2. `set` 操作：修改允许的配置项（如 timeout、max_output 等）
3. 安全机制：BLOCKED（禁止访问）、READ_ONLY（只读）、敏感字段过滤
4. 通过 ToolContext 传递运行时状态引用（简化版，不需要 AgentLoop 直接引用）

## 3. 非目标

- 不实现完整的 AgentLoop 引用传递
- 不实现子代理状态查看
- 不实现 MCP 服务器状态
- 不实现复杂的嵌套属性访问

## 4. 验收标准

1. MyTool 可被 ToolLoader 发现
2. get 操作能查看 workspace 和 config
3. set 操作能修改允许的配置
4. BLOCKED 属性被拒绝访问
5. READ_ONLY 属性被拒绝修改
6. 敏感字段（api_key/password 等）被过滤
7. 单元测试通过
