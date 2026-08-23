# Step 75 Design: MyTool

## 1. 架构

```
tools/self.py（新建）
  ├── MyToolConfig        配置（enable, allow_set）
  └── MyTool(Tool)        运行时自省工具
```

## 2. 安全边界

- **BLOCKED**：禁止 get 和 set（bus, provider, tools, sessions 等核心基础设施）
- **READ_ONLY**：允许 get 但禁止 set（workspace, config, exec_config 等）
- **_SENSITIVE_NAMES**：字段名含 api_key/secret/password/token 等时过滤值
- **_DENIED_ATTRS**：禁止访问 Python 内部属性（__class__, __dict__ 等）

## 3. 运行时状态传递

简化版：通过 ToolContext 的 `runtime_state` 字段传递一个 dict，包含可查看/修改的运行时属性。
- `workspace`：当前 workspace 路径（只读）
- `config`：配置对象（部分可修改）
- `tools`：已注册工具列表（只读）
- `iteration`：当前迭代次数（只读）

## 4. 参数

```
action: "get" | "set"  # 必填
key: str               # 属性名（必填）
value: any             # set 时的值
```

## 5. 执行流程

1. 校验 action 和 key
2. 检查 key 是否在 _DENIED_ATTRS 或 BLOCKED 中
3. get：从 runtime_state 获取值，过滤敏感字段，返回 JSON
4. set：检查 allow_set 配置、READ_ONLY、敏感字段，设置值

## 6. 测试策略

- get workspace
- get config
- set 允许的配置
- BLOCKED 拒绝
- READ_ONLY 拒绝修改
- 敏感字段过滤
- allow_set=False 时拒绝所有 set
