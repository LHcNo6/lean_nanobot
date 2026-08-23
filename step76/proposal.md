# Step 76 Proposal: ReadFileTool 升级迁移

## 1. 问题背景

当前 ReadFileTool 在独立文件 `tools/read_file.py`，只支持 path/max_chars，没有行号分页。
nanobot 的 ReadFileTool 在 `tools/filesystem.py` 中，继承 _FsTool，支持 offset/limit 行号分页，
输出格式为 `LINE_NUM|CONTENT`，与 edit_file/apply_patch 的行号引用一致。

## 2. 目标

1. 在 `tools/filesystem.py` 中添加升级后的 ReadFileTool（继承 _FsTool）
2. 支持 offset/limit 行号分页参数
3. 输出格式：`LINE_NUM|CONTENT`
4. 保留 max_chars 截断
5. 删除旧的 `tools/read_file.py`（或改为 re-export 向后兼容）
6. 集成 FileStates 读取追踪

## 3. 非目标

- 不实现 PDF/Office 文档读取
- 不实现图片读取
- 不实现去重（dedup）
- 不实现 force 参数

## 4. 验收标准

1. ReadFileTool 在 filesystem.py 中，继承 _FsTool
2. offset/limit 行号分页工作正常
3. 输出格式为 `N|content`
4. 旧的 read_file.py 被移除或兼容
5. 单元测试通过
