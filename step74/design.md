# Step 74 Design: ApplyPatchTool

## 1. 架构

```
tools/apply_patch.py（新建）
  ├── _PatchSummary dataclass    补丁摘要（action/path/added/deleted）
  ├── _PatchError                补丁错误异常
  ├── 辅助函数（_validate_patch_path/_append_text/_line_diff_stats/_format_summary）
  └── ApplyPatchTool(_FsTool)    统一补丁应用工具
```

## 2. 参数

```python
edits: list[dict]  # 必填，1-20个edit
  each edit:
    path: str       # 必填，文件路径
    action: str     # 必填，"replace" 或 "add"
    old_text: str   # replace 必填，要替换的精确文本
    new_text: str   # 必填，替换文本或追加文本
dry_run: bool = False  # 只验证不写入
```

## 3. 执行流程

1. 校验 edits 非空、每个 edit 是 dict、有 path 和 action
2. 遍历 edits，累积到 `writes: dict[Path, str]`（支持同文件多 edit 链式应用）
3. replace：读取文件 → 精确查找 old_text（唯一匹配）→ 替换
4. add：读取文件（不存在则空）→ 追加 new_text（保留换行符）
5. dry_run：返回摘要，不写入
6. 原子写入：备份所有文件 → 写入 → 失败则回滚
7. 返回格式化摘要

## 4. 关键设计

- **链式编辑**：同文件的多个 edit 按顺序应用，前一个的输出是后一个的输入
- **CRLF 保留**：检测原文件是否用 CRLF，输出时保持一致
- **唯一匹配**：replace 的 old_text 必须在文件中唯一出现，否则报错
- **原子性**：所有文件先备份，全部写入成功才提交，失败则回滚
- **diff 统计**：用 difflib.SequenceMatcher 计算新增/删除行数

## 5. 测试策略

- replace 单文件
- add 追加到现有文件
- add 创建新文件
- 多文件批量编辑
- dry_run 不写入
- old_text 多处匹配报错
- old_text 不存在报错
- CRLF 保留
- 原子性（部分失败回滚）
