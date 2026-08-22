# step102：build_dream_prompt 模板化

## 解决的问题

build_dream_prompt 使用硬编码的系统提示，未使用 step101 新增的 _dream_template()。

## 实现

build_dream_prompt 中硬编码的 "You are a memory curator..." 替换为 `self._dream_template()`，模板内容作为前缀，后面追加 files_section 和 Conversation History。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：build_dream_prompt 改用 _dream_template() |
| `tests/test_build_dream_prompt.py` | 新建（6 测试） |
| 规范文档 + step102.md | 新建 |

## 测试结果

6 passed in 0.46s

## 下一步

**step103**：`dream_session_key()` + `prune_dream_sessions()` + main.py 集成。
