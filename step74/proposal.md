# Step 74 Proposal: ApplyPatchTool 统一补丁应用

## 1. 问题背景

step66 的 EditFileTool 只支持单文件精确字符串替换。批量修改多个文件时需要多次调用，
且不支持追加/创建操作。nanobot 的 ApplyPatchTool 支持多文件批量编辑，是代码修改的默认工具。

## 2. 目标

新建 `tools/apply_patch.py`，实现 ApplyPatchTool：
1. 多文件批量编辑（单次调用最多 20 个 edit）
2. 两种操作：`replace`（精确替换）和 `add`（追加/创建）
3. `dry_run` 模式（验证+预览，不写入）
4. CRLF 换行符保留
5. 原子写入（备份+回滚）
6. diff 统计（+added/-deleted）
7. 继承 _FsTool，复用路径解析和 workspace 边界

## 3. 非目标

- 不实现 unified diff 格式解析
- 不实现正则替换
- 不实现行号范围替换
- 不实现删除文件操作

## 4. 验收标准

1. ApplyPatchTool 可被 ToolLoader 发现
2. replace 操作精确替换单处文本
3. add 操作追加到现有文件或创建新文件
4. 多文件批量编辑原子性（部分失败不写入）
5. dry_run 不写入文件
6. old_text 多处匹配时报错
7. CRLF 文件保留换行符
8. 单元测试通过
