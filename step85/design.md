# Step 85 Design: 工具注册整合

## 1. 架构

```
loader.py（修改）
  └── _SKIP_MODULES  检查并更新跳过列表

tests/test_tool_discovery.py（新建）
  ├── 验证 ToolLoader.discover() 返回所有预期工具类
  ├── 验证新增工具（CliAppsTool、ListExecSessionsTool）被发现
  ├── 验证工具注册流程
  └── 验证 _plugin_discoverable 标志
```

## 2. _SKIP_MODULES 检查

当前跳过列表：
```python
_SKIP_MODULES = {
    "base", "schema", "registry", "context", "loader", "config",
    "file_state", "sandbox", "mcp", "__init__",
}
```

分析：
- base/schema/registry/context/loader/config：基础模块，无工具类 ✓
- file_state：文件状态存储，无工具类 ✓
- sandbox：沙箱后端，无工具类 ✓
- mcp：MCP 工具是动态创建的，不应通过类发现 ✓
- __init__：包初始化 ✓

结论：_SKIP_MODULES 合理，无需修改。

## 3. 预期工具类列表

通过 ToolLoader 应发现的工具类：
- ApplyPatchTool
- CliAppsTool（step83 新增）
- CronTool
- EchoTool
- FindFilesTool
- GlobTool
- GrepTool
- ImageGenerationTool
- ListExecSessionsTool（step84 新增）
- LongTaskTool / GoalTool
- MessageTool
- MyTool
- ReadFileTool
- SpawnTool
- WebFetchTool
- WebSearchTool
- WriteStdinTool
- WriteTool / EditTool
- ExecTool

## 4. 测试策略

1. ToolLoader.discover() 返回非空列表
2. 新增工具类在发现列表中
3. 每个工具类有 name 属性
4. 工具类按名称排序
5. 注册流程：load() 能成功注册工具
6. _plugin_discoverable=False 的类不被发现
