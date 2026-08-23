# Step 77 Design: GlobTool

## 1. 架构

```
tools/glob_tool.py（新建）
  └── GlobTool(_FsTool)  glob 模式匹配工具
```

## 2. 参数

```python
pattern: str       # glob 模式（必填）
path: str = "."    # 搜索起始路径
max_results: int = 200  # 最大结果数
```

## 3. 执行流程

1. 校验 pattern 非空
2. _resolve_read 解析起始路径
3. 用 pathlib.Path.glob(pattern) 匹配
4. 过滤掉目录（只返回文件）或同时返回目录
5. 转换为相对路径（as_posix）
6. 限制 max_results
7. 返回格式化结果

## 4. 输出格式

```
Found N files matching 'pattern':
  path/to/file1.py
  path/to/file2.py
```

## 5. 测试策略

- `*.py` 匹配当前目录
- `**/*.py` 递归匹配
- `test_*.py` 前缀匹配
- 空结果
- max_results 限制
- 工具发现
