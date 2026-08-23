# step66：EditFileTool 精确字符串替换

## 1. 问题背景

step65 引入了 `WriteFileTool`，agent 可以创建和覆盖文件。但对于代码修改场景，
整文件覆盖效率低且风险大——agent 只想替换一小段代码，却需要重写整个文件，
容易引入意外修改。

nanobot 的 `EditFileTool` 提供精确字符串替换能力：agent 从 `read_file` 输出中
复制 `old_text`，提供 `new_text`，工具只替换匹配部分。这是代码编辑的核心工具，
也是 agent "读-改-写"闭环的关键一环。

## 2. 原理分析

### 2.1 为什么用 `str.find()` 循环而不是 `str.replace()`？

`str.replace(old, new)` 会替换所有匹配，无法：
- 选择第 N 个匹配（`occurrence` 参数）；
- 在多匹配时返回歧义警告（不执行替换）；
- 记录匹配的行号（用于错误提示）。

`str.find()` 循环查找所有匹配位置，记录为 `_MatchSpan` 列表（含 start/end/text/line），
然后根据参数选择要替换的匹配，倒序替换避免位置偏移。这是 nanobot 采用的方案。

### 2.2 为什么倒序替换？

如果正序替换，第一个匹配替换后，后续匹配的 start/end 偏移会失效（因为 new_text
长度可能与 old_text 不同）。倒序替换时，先替换后面的匹配，前面的匹配位置不受影响。

### 2.3 为什么多匹配时不默认替换第一个？

如果 `old_text` 在文件中出现多次，盲目替换第一个可能改错位置。nanobot 的设计是：
- 匹配数 = 1：直接替换；
- 匹配数 > 1 且无 `replace_all`/`occurrence`：返回歧义警告，要求 agent 提供更多
  上下文或明确指定替换哪个。

这减少了 agent 误操作的风险。

### 2.4 为什么需要 read-before-edit 警告？

agent 可能基于过期的文件内容进行编辑（例如文件被外部修改后）。`FileStates.check_read`
检查文件是否在当前会话中读取过、读取后是否被修改，在编辑前给出警告。警告不阻止编辑，
但提醒 agent 确认内容。

### 2.5 为什么保留 CRLF？

Windows 下很多文件使用 CRLF 换行。如果编辑后统一转 LF，会导致整个文件的换行符
被修改，产生无意义的 diff。因此检测原文件的换行风格，编辑后还原。

## 3. 实现方案

### 3.1 匹配辅助（`tools/filesystem.py`）

```python
@dataclass(slots=True)
class _MatchSpan:
    start: int    # 0-indexed 偏移
    end: int      # exclusive
    text: str     # 匹配到的实际文本
    line: int     # 1-indexed 行号

def _find_matches(content: str, old_text: str) -> list[_MatchSpan]:
    # str.find() 循环查找，步进 max(1, len(old_text)) 避免空串无限循环
```

### 3.2 EditFileTool 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 要编辑的文件路径 |
| `old_text` | string | 是 | 要替换的精确文本 |
| `new_text` | string | 是 | 替换文本 |
| `replace_all` | boolean | 否 | 替换所有匹配 |
| `occurrence` | integer | 否 | 替换第 N 个匹配（1-indexed） |

### 3.3 执行流程

1. 参数校验（非空、occurrence >= 1、replace_all 与 occurrence 互斥）
2. `_resolve_write(path)` 解析路径（边界守卫）
3. 文件不存在 → 错误
4. 读取文件，检测 CRLF，统一转 LF
5. `_file_states.check_read(fp)` 获取警告
6. `_find_matches(content, old_text)` 查找所有匹配
7. 无匹配 → 错误
8. 选择匹配（replace_all / occurrence / 单匹配 / 多匹配歧义）
9. 倒序替换
10. CRLF 还原，写回文件
11. `_file_states.record_write(fp)`
12. 返回结果（含警告）

## 4. 核心类/函数说明

### `_MatchSpan`（dataclass）

单个 old_text 匹配的位置信息。`line` 字段用于歧义警告和错误提示，让 agent 知道
匹配出现在哪些行。

### `_find_matches(content, old_text)`

精确匹配查找函数。使用 `str.find()` 循环，步进 `max(1, len(old_text))` 避免空字符串
导致的无限循环。返回按出现顺序排列的 `_MatchSpan` 列表。

### `EditFileTool`

精确字符串替换工具，继承 `_FsTool`。

关键特性：
- 支持 `replace_all` 和 `occurrence` 两种匹配选择方式；
- 多匹配歧义时返回警告（不执行替换）；
- 集成 read-before-edit 警告；
- 保留原文件的 CRLF/LF 换行风格；
- 倒序替换避免位置偏移。

## 5. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tools/filesystem.py` | 修改 | 新增 `_MatchSpan`、`_find_matches`、`EditFileTool`；更新导入（BooleanSchema/IntegerSchema/dataclass） |
| `tests/test_edit_file.py` | 新建 | 26 个单元测试 |
| `proposal.md` | 新建 | 需求定义 |
| `design.md` | 新建 | 架构设计 |
| `api-spec.md` | 新建 | 接口契约 |

## 6. 测试结果

- `tests/test_edit_file.py`：26 passed（匹配查找/基础替换/歧义/错误/警告/CRLF/发现）
- `tests/test_filesystem.py`：19 passed（无回归）
- `tests/test_file_state.py`：21 passed（无回归）
- `tests/test_workspace_tool.py`：18 passed（无回归）

## 7. 暴露问题与下一步

### 7.1 暴露的技术债

1. **缺少高级匹配特性**：nanobot 的 EditFileTool 还支持 `line_hint`（按行号定位）、
   `expected_replacements`（断言替换数）、引号风格保留（`_preserve_quote_style`）、
   缩进风格保留（`_reindent_like_match`）、最佳匹配诊断（`_best_window` + unified diff）。
   step66 简化版未实现这些，后续可按需增强。

2. **缺少创建文件语义**：nanobot 支持 `old_text=''` + 文件不存在时创建新文件。
   step66 未实现，agent 应使用 `write_file` 创建文件。

3. **缺少删除行尾随换行清理**：nanobot 在删除文本（`new_text=''`）时会消费尾随换行，
   避免留下空行。step66 未实现，删除后可能留下空行。

4. **缺少文件大小保护**：nanobot 限制编辑文件不超过 1 GiB。step66 未实现，
   当前场景文件不大，风险可控。

5. **ReadFileTool 仍在独立文件**：`tools/read_file.py` 与 `tools/filesystem.py`
   并存，路径解析逻辑有重复。

### 7.2 下一步（step67）

**ListDirTool**：在 `tools/filesystem.py` 中新增 `ListDirTool`，实现目录列表功能。
支持 `path`、`recursive`、`max_entries` 参数，返回格式化的目录内容。依赖 step65 的
`_FsTool` 基类。
