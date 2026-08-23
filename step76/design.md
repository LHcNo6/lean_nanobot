# Step 76 Design: ReadFileTool 升级迁移

## 1. 架构

```
tools/filesystem.py（修改）
  └── +ReadFileTool(_FsTool)  升级版：行号分页 + LINE_NUM|CONTENT 格式

tools/read_file.py（删除）
  └── 旧版 ReadFileTool 移除
```

## 2. 参数

```python
path: str       # 必填，文件路径
offset: int = 1 # 起始行号（1-based）
limit: int | None = None  # 返回行数（None=不限制）
max_chars: int = 60000  # 最大字符数
```

## 3. 输出格式

```
1|第一行内容
2|第二行内容
3|第三行内容
```

空行：`5|`

## 4. 执行流程

1. 校验 path 非空
2. _resolve_read 解析路径（继承 _FsTool）
3. 检查文件存在且是文件
4. 读取文件内容（UTF-8）
5. 按行分割，应用 offset/limit
6. 格式化为 `N|content`
7. max_chars 截断
8. 记录 FileStates.read（集成文件状态追踪）

## 5. 向后兼容

- 删除 tools/read_file.py
- ToolLoader 从 filesystem.py 发现 ReadFileTool
- 工具名仍为 `read_file`，参数新增 offset/limit（旧参数 path/max_chars 保留）

## 6. 测试策略

- 读取完整文件
- offset 分页
- limit 限制行数
- offset+limit 组合
- 空文件
- 不存在的文件
- max_chars 截断
- 输出格式验证
- 工具发现（从 filesystem.py）
