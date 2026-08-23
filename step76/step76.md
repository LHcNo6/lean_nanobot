# step76：ReadFileTool 升级迁移

## 实现

在 `tools/filesystem.py` 中添加升级版 ReadFileTool（继承 _FsTool）：
- 新增 offset/limit 行号分页参数
- 输出格式：`LINE_NUM|CONTENT`
- max_chars 截断
- 分页信息提示
- 旧的 `tools/read_file.py` 改为 re-export 向后兼容

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/filesystem.py` | 修改：+ReadFileTool |
| `tools/read_file.py` | 修改：改为 re-export |
| `tests/test_read_file_v2.py` | 新建（13测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

13 passed
