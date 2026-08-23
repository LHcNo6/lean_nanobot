# Step 66 Design: EditFileTool

## 1. 架构概览

```
tools/filesystem.py
  ├── _FsTool(Tool)              [step65 已有]
  ├── WriteFileTool(_FsTool)     [step65 已有]
  ├── _MatchSpan                  [step66 新增] dataclass: 匹配位置信息
  ├── _find_matches()             [step66 新增] 函数: 精确匹配查找
  └── EditFileTool(_FsTool)       [step66 新增] 精确字符串替换工具
```

## 2. 模块详细设计

### 2.1 `_MatchSpan`（dataclass）

```python
@dataclass(slots=True)
class _MatchSpan:
    """单个 old_text 匹配的位置信息。"""
    start: int   # 在 content 中的起始偏移（0-indexed）
    end: int     # 结束偏移（exclusive）
    text: str    # 匹配到的实际文本
    line: int    # 起始行号（1-indexed，用于错误提示）
```

### 2.2 `_find_matches(content, old_text)`

```python
def _find_matches(content: str, old_text: str) -> list[_MatchSpan]:
    """在 content 中查找所有 old_text 的精确匹配，返回位置列表。

    使用 str.find() 循环查找，每次找到后从 idx+max(1,len(old_text)) 继续，
    避免空字符串匹配导致的无限循环。
    """
    matches: list[_MatchSpan] = []
    start = 0
    while True:
        idx = content.find(old_text, start)
        if idx == -1:
            break
        matches.append(_MatchSpan(
            start=idx,
            end=idx + len(old_text),
            text=content[idx:idx + len(old_text)],
            line=content.count("\n", 0, idx) + 1,
        ))
        start = idx + max(1, len(old_text))
    return matches
```

### 2.3 `EditFileTool`

#### 参数 Schema

```python
@tool_parameters(tool_parameters_schema(
    path=StringSchema("The file path to edit"),
    old_text=StringSchema("The exact text to replace (copy from read_file output)"),
    new_text=StringSchema("The replacement text"),
    replace_all=BooleanSchema("Replace all occurrences (default false)"),
    occurrence=IntegerSchema(
        "Replace the Nth occurrence (1-indexed); cannot use with replace_all",
        minimum=1,
    ),
    required=["path", "old_text", "new_text"],
))
```

#### execute 流程

```
1. 参数校验
   ├─ path 为空 → error
   ├─ old_text 为 None → error
   ├─ new_text 为 None → error
   ├─ occurrence < 1 → error
   └─ replace_all + occurrence 同时提供 → error

2. 路径解析：fp = self._resolve_write(path)

3. 文件存在性检查
   └─ 不存在 → error("File not found")

4. 读取文件
   ├─ raw = fp.read_bytes()
   ├─ uses_crlf = b"\r\n" in raw
   └─ content = raw.decode("utf-8").replace("\r\n", "\n")

5. read-before-edit 检查
   └─ warning = self._file_states.check_read(fp)

6. 匹配查找
   ├─ norm_old = old_text.replace("\r\n", "\n")
   ├─ matches = _find_matches(content, norm_old)
   └─ 无匹配 → error("old_text not found")

7. 匹配选择
   ├─ replace_all=True → selected = all matches
   ├─ occurrence=N → selected = [matches[N-1]]（越界 → error）
   ├─ 匹配数=1 → selected = [matches[0]]
   └─ 匹配数>1 且无参数 → warning（列出行号，不执行替换）

8. 执行替换（倒序，避免位置偏移）
   ├─ norm_new = new_text.replace("\r\n", "\n")
   └─ for match in reversed(selected):
         content = content[:match.start] + norm_new + content[match.end:]

9. 写回文件
   ├─ uses_crlf → content = content.replace("\n", "\r\n")
   ├─ fp.write_bytes(content.encode("utf-8"))
   └─ self._file_states.record_write(fp)

10. 返回结果
    └─ warning ? f"{warning}\nSuccessfully edited {fp}" : f"Successfully edited {fp}"
```

#### 关键方法

| 方法 | 说明 |
|------|------|
| `execute(path, old_text, new_text, replace_all, occurrence, **kwargs)` | 主执行方法 |
| `name` (property) | 返回 `"edit_file"` |
| `description` (property) | 工具描述 |
| `read_only` (property) | 返回 `False` |

## 3. 数据流向

```
agent 调用 edit_file(path="a.py", old_text="foo", new_text="bar")
  → _resolve_write(path) → Path（边界守卫）
  → fp.read_bytes() → raw
  → content = raw.decode().replace("\r\n", "\n")
  → _file_states.check_read(fp) → warning | None
  → _find_matches(content, "foo") → [MatchSpan(...), ...]
  → 选择匹配 → selected
  → 倒序替换 → new_content
  → uses_crlf ? new_content.replace("\n", "\r\n")
  → fp.write_bytes(new_content.encode())
  → _file_states.record_write(fp)
  → 返回 ToolResult
```

## 4. 错误处理

| 场景 | 返回消息 |
|------|---------|
| 空 path | `Error: edit_file requires a 'path' parameter.` |
| None old_text | `Error: edit_file requires an 'old_text' parameter.` |
| None new_text | `Error: edit_file requires a 'new_text' parameter.` |
| occurrence < 1 | `Error: occurrence must be >= 1.` |
| replace_all + occurrence | `Error: occurrence cannot be used with replace_all=true.` |
| 文件不存在 | `Error: File not found: {path}` |
| old_text 未找到 | `Error: old_text not found in {path}. Verify the file content.` |
| occurrence 越界 | `Error: occurrence {N} is out of range; old_text appears {count} time(s).` |
| 多匹配歧义 | `Warning: old_text appears {count} times at line {n1}, line {n2}, ... Provide more context, set occurrence, or set replace_all=true.` |
| PermissionError | `Error: {exc}` |
| OSError | `Error editing file: {exc}` |

## 5. 安全边界

- **路径越界**：复用 `_FsTool._resolve_write`，受限模式下强制路径在 workspace 内
- **编码**：固定 UTF-8 读取/写入，与 WriteFileTool 一致
- **无 shell 注入**：纯文件操作，不执行命令
- **read-before-edit**：编辑前检查文件是否已读取，减少覆盖未查看内容的风险

## 6. 测试策略

### 单元测试 `tests/test_edit_file.py`
1. `test_single_match_replace`：单匹配替换成功
2. `test_replace_all`：replace_all 替换所有匹配
3. `test_occurrence_select`：occurrence 选择第 N 个匹配
4. `test_multiple_matches_ambiguous`：多匹配无参数返回警告
5. `test_old_text_not_found`：old_text 未找到返回错误
6. `test_file_not_found`：文件不存在返回错误
7. `test_read_before_edit_warning`：未读取文件时编辑返回警告
8. `test_crlf_preserved`：CRLF 文件编辑后保持 CRLF
9. `test_replace_all_and_occurrence_mutually_exclusive`：两参数互斥
10. `test_occurrence_out_of_range`：occurrence 越界返回错误
11. `test_empty_old_text`：空 old_text 行为（匹配所有位置，应拒绝）
12. `test_tool_discovered_by_loader`：ToolLoader 自动发现
13. `test_tool_schema`：参数 schema 正确
