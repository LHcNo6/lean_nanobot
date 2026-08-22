# Step 92 Design: MemoryStore 持久化文件读写

## 1. 原理分析

### 1.1 为什么需要统一的 read_file

三个持久化文件（MEMORY.md、SOUL.md、USER.md）都是可选的——新 workspace
初始化时可能不存在。如果每个 read 方法都各自 try-except FileNotFoundError，
会产生重复代码。参考实现提取了 `read_file` 静态方法，统一处理"文件不存在
返回空串"的语义，其他 read 方法直接委托。

### 1.2 方法设计

```
read_file(path) -> str          # 静态，通用读取
read_memory() -> str            # 委托 read_file(self.memory_file)
write_memory(content) -> None   # self.memory_file.write_text(content, encoding="utf-8")
read_soul() -> str              # 委托 read_file(self.soul_file)
write_soul(content) -> None     # self.soul_file.write_text(content, encoding="utf-8")
read_user() -> str              # 委托 read_file(self.user_file)
write_user(content) -> None     # self.user_file.write_text(content, encoding="utf-8")
```

### 1.3 与参考实现的对齐点

参考实现 `nanobot/agent/memory.py`：
- `read_file` 是 `@staticmethod`，捕获 `FileNotFoundError` 返回 `""`
- 六个读写方法均为一行委托，逻辑极简
- write 方法直接 `path.write_text(content, encoding="utf-8")`，不做原子写
  （原子写是 step94 针对 history.jsonl 的优化，持久化文件保持简单覆盖）

## 2. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `memory.py` | 修改 | 新增 read_file 静态方法 + 6 个读写方法 |
| `tests/test_memory_store.py` | 新建 | 单元测试 |
| `proposal.md` | 新建 | 需求定义 |
| `design.md` | 新建 | 架构设计 |
| `api-spec.md` | 新建 | 接口契约 |
| `step92.md` | 新建 | 配套文档 |
