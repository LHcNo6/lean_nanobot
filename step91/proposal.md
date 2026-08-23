# Step 91 Proposal: MyTool 完整 AgentLoop 引用 + 嵌套属性访问

## 1. 问题背景

step75 的 MyTool 只支持单层属性访问（如 `workspace`、`config`），
不支持嵌套路径（如 `config.exec.timeout`），也没有 AgentLoop 引用。
nanobot 的 MyTool 支持完整的 AgentLoop 自省和嵌套属性访问。

## 2. 目标

增强 `tools/self.py`：
1. ToolContext 新增 agent_loop 字段
2. MyTool 支持嵌套属性访问（点分路径，如 `config.exec.timeout`）
3. 新增 `agent` 顶级 key，映射到 agent_loop
4. 嵌套路径逐段检查安全边界（BLOCKED / _DENIED_ATTRS）
5. 敏感字段过滤在嵌套访问中仍然生效

## 3. 非目标

- 不实现嵌套属性的 set（只支持 get）
- 不实现 AgentLoop 的方法调用
- 不实现子代理状态访问

## 4. 验收标准

1. 嵌套属性 get 工作（如 `config.exec.timeout`）
2. `agent` key 映射到 agent_loop
3. 嵌套路径中遇到 BLOCKED 属性报错
4. 嵌套路径中遇到 Python 内部属性报错
5. 敏感字段在嵌套结果中被过滤
6. 向后兼容：单层属性仍然工作
7. 单元测试通过
