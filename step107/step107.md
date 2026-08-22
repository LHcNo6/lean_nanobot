# step107：build_dream_tools

## 解决的问题

缺少 Dream 运行使用的受限工具定义，Dream 无法通过工具调用修改记忆文件。

## 实现

新增 `build_dream_tools()` 方法，返回 OpenAI 格式的工具定义列表，包含：
- read_file：读取工作区文件
- write_file：写入文件
- edit_file：替换文件内容

不包含 shell/exec/网络等危险工具。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+build_dream_tools 方法 |
| `tests/test_build_dream_tools.py` | 新建（6 测试） |
| 规范文档 + step107.md | 新建 |

## 测试结果

6 passed in 0.29s

## 下一步

**step108**：Legacy HISTORY.md → history.jsonl 迁移。
