# Step 66 Proposal: EditFileTool

## 1. 问题背景

step65 引入了 `WriteFileTool`，agent 可以创建和覆盖文件。但对于代码修改场景，
整文件覆盖效率低且风险大——agent 只想替换一小段代码，却需要重写整个文件。

nanobot 的 `EditFileTool` 提供精确字符串替换能力：agent 从 `read_file` 输出中
复制 `old_text`，提供 `new_text`，工具只替换匹配部分。这是代码编辑的核心工具。

## 2. 目标

在 `tools/filesystem.py` 中新增 `EditFileTool`，实现精确字符串替换：

1. 核心参数：`path`、`old_text`、`new_text`、`replace_all`、`occurrence`
2. 精确匹配查找所有 `old_text` 出现位置
3. 多匹配时的歧义处理（要求 `replace_all` 或 `occurrence`）
4. 集成 `FileStates.check_read` 实现 read-before-edit 警告
5. CRLF 换行符保留
6. 文件不存在 / old_text 未找到的清晰错误消息

## 3. 非目标（明确不做）

- **不实现** `line_hint` 参数（按行号定位匹配）—— 后续增强
- **不实现** `expected_replacements` 参数 —— 后续增强
- **不实现** 引号风格保留（`_preserve_quote_style`）—— LLM 输出通常用直引号
- **不实现** 缩进风格保留（`_reindent_like_match`）—— 后续增强
- **不实现** 最佳匹配诊断（`_best_window` + unified diff）—— 简化错误消息
- **不实现** 创建文件语义（`old_text=''` 创建新文件）—— 用 `write_file` 即可
- **不实现** 删除行尾随换行清理 —— 后续增强
- **不实现** Markdown 尾随空格保留 —— 后续增强
- **不实现** 文件大小保护（1 GiB）—— 当前场景文件不大

## 4. 方案选择

### 方案 A：直接用 `str.replace()`
最简单的实现，`content.replace(old_text, new_text)`。
- 优点：代码极简
- 缺点：无法处理多匹配歧义、无法选择第 N 个匹配、无法记录行号

### 方案 B：`str.find()` 循环查找 + 选择性替换（选定）
用 `str.find()` 循环查找所有匹配位置，记录为 `_MatchSpan` 列表，然后根据
`replace_all`/`occurrence` 选择要替换的匹配，倒序替换避免位置偏移。
- 优点：支持多匹配歧义处理、occurrence 选择、行号提示
- 缺点：比方案 A 多约 30 行代码

**选择方案 B**。多匹配歧义处理是 EditFileTool 的核心价值——如果 old_text 出现
多次，盲目替换第一个可能改错位置。nanobot 也采用此方案。

## 5. 关键设计决策

### 5.1 匹配查找：`_find_matches`
```python
@dataclass(slots=True)
class _MatchSpan:
    start: int
    end: int
    text: str
    line: int  # 1-indexed

def _find_matches(content: str, old_text: str) -> list[_MatchSpan]:
    matches = []
    start = 0
    while True:
        idx = content.find(old_text, start)
        if idx == -1:
            break
        matches.append(_MatchSpan(
            start=idx, end=idx + len(old_text),
            text=content[idx:idx + len(old_text)],
            line=content.count("\n", 0, idx) + 1,
        ))
        start = idx + max(1, len(old_text))
    return matches
```

### 5.2 多匹配歧义处理
- `replace_all=True`：替换所有匹配
- `occurrence=N`：替换第 N 个匹配（1-indexed）
- 两者都不提供且匹配数 > 1：返回警告，列出前 3 个匹配的行号，要求 agent 提供
  更多上下文或设置 `replace_all`/`occurrence`
- `replace_all` 与 `occurrence` 互斥

### 5.3 read-before-edit 警告
在执行替换前调用 `self._file_states.check_read(fp)`：
- 文件未读取过 → 返回 "Warning: file has not been read yet..."
- 文件读取后被外部修改 → 返回 "Warning: file has been modified since last read..."
- 警告附加在成功消息前面，不阻止编辑

### 5.4 CRLF 处理
- 读取时检测 `b"\r\n" in raw`，记录 `uses_crlf`
- 内容统一转 `\n` 处理
- 写回时如果原文件用 CRLF，转 `\r\n`

## 6. 验收标准

1. `EditFileTool` 可被 `ToolLoader` 自动发现并注册
2. 单匹配替换成功，内容正确
3. `replace_all=True` 替换所有匹配
4. `occurrence=N` 替换第 N 个匹配
5. 多匹配无参数时返回歧义警告（不执行替换）
6. `old_text` 未找到返回清晰错误
7. 文件不存在返回错误
8. read-before-edit 警告正常工作
9. CRLF 文件编辑后保持 CRLF
10. 所有现有测试通过，新增 `EditFileTool` 单元测试
