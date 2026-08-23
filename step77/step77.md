# step77：GlobTool

## 实现

新建 `tools/glob_tool.py`，继承 _FsTool：
- 标准 glob 模式匹配（*, ?, **, [seq]）
- pathlib.Path.glob 实现
- 相对路径输出（as_posix）
- max_results 限制

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/glob_tool.py` | 新建 |
| `tests/test_glob_tool.py` | 新建（8测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

8 passed
