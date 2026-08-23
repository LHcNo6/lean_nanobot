# Step 85 Proposal: 工具注册整合 + 发现验证

## 1. 问题背景

step83 新增了 CliAppsTool，step84 新增了 ListExecSessionsTool。
需要确保这些新工具能被 ToolLoader 自动发现并注册到 ToolRegistry 中。
同时验证所有已有工具类的发现状态，修复发现问题。

## 2. 目标

1. 验证 ToolLoader 能正确发现所有工具类（含新增的 CliAppsTool、ListExecSessionsTool）
2. 检查 _SKIP_MODULES 是否合理，移除不必要的跳过
3. 添加工具发现集成测试：验证所有预期工具类都能被发现
4. 确保工具注册流程完整（discover → enabled → create → register）

## 3. 非目标

- 不实现新的工具类
- 不修改工具的业务逻辑
- 不实现 MCP 工具的自动发现（MCP 工具是动态创建的）

## 4. 验收标准

1. ToolLoader.discover() 返回所有预期工具类
2. CliAppsTool 和 ListExecSessionsTool 在发现列表中
3. 工具发现集成测试通过
4. 注册流程验证通过
5. 单元测试通过
