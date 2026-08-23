# Step 74 API Specification

## 1. ApplyPatchTool API

**文件**：`tools/apply_patch.py`
**继承**：`_FsTool`

| 属性 | 值 |
|------|-----|
| `name` | `"apply_patch"` |
| `_scopes` | `{"core", "subagent"}` |
| `read_only` | `False` |

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `edits` | array[object] | 是 | 1-20个编辑操作 |
| `edits[].path` | string | 是 | 文件路径 |
| `edits[].action` | string | 是 | `"replace"` 或 `"add"` |
| `edits[].old_text` | string | replace必填 | 要替换的精确文本 |
| `edits[].new_text` | string | 是 | 替换/追加文本 |
| `dry_run` | boolean | 否 | 只验证不写入（默认false） |

### 返回值

成功：
```
Patch applied:
- update path/to/file.py (+3/-2)
- add new_file.py (+10/-0)
```

dry_run：
```
Patch dry-run succeeded:
- update path/to/file.py (+3/-2)
```

失败：`ToolResult.error("Error: {message}")`

## 2. 错误场景

| 场景 | 错误消息 |
|------|---------|
| 空 edits | `must provide edits` |
| 缺 path | `path required for edit` |
| 缺 action | `action required for edit: {path}` |
| 未知 action | `unknown action: {action}` |
| replace 缺 old_text | `old_text required for replace: {path}` |
| old_text 不存在 | `old_text not found in {path}` |
| old_text 多处匹配 | `old_text appears multiple times in {path}` |
| 文件不存在(replace) | `file to update does not exist: {path}` |
| 非UTF-8文件 | `file is not UTF-8 text: {path}` |

## 3. 工具发现契约

`ToolLoader` 扫描 `tools/apply_patch.py` 时发现 `ApplyPatchTool`。
最终注册：`apply_patch`
