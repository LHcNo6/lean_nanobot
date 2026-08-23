# step74：ApplyPatchTool 统一补丁应用

## 实现

新建 `tools/apply_patch.py`，继承 _FsTool：
- 多文件批量编辑（1-20个edit）
- replace：精确替换唯一匹配的 old_text
- add：追加到现有文件或创建新文件
- dry_run：验证+预览不写入
- CRLF 保留
- 原子写入（备份+回滚）
- difflib 统计 +added/-deleted

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/apply_patch.py` | 新建 |
| `tests/test_apply_patch.py` | 新建（16测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

16 passed
