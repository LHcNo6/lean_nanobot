# step75：MyTool 运行时自省

## 实现

新建 `tools/self.py`：
- get 操作：查看 workspace/config/exec_timeout/web_timeout 等
- set 操作：修改 exec_timeout/web_timeout（需 allow_set=True）
- 安全边界：BLOCKED/READ_ONLY/_DENIED_ATTRS/敏感字段过滤
- 配置：MyToolConfig（enable, allow_set）

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `config/schema.py` | 修改：+MyToolConfig + ToolsConfig.my |
| `tools/self.py` | 新建 |
| `tests/test_my_tool.py` | 新建（18测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

18 passed
