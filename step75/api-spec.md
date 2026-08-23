# Step 75 API Specification

## 1. MyTool API

**文件**：`tools/self.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"my"` |
| `config_key` | `"my"` |
| `_scopes` | `{"core"}` |

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | 是 | `"get"` 或 `"set"` |
| `key` | string | 是 | 属性名 |
| `value` | any | set时 | 要设置的值 |

### 返回值

get：JSON 字符串 `{"key": ..., "value": ...}`
set：JSON 字符串 `{"key": ..., "value": ..., "status": "set"}`
失败：`ToolResult.error(...)`

## 2. 安全分类

### BLOCKED（禁止 get/set）
bus, provider, tools, sessions, runner, context, _running, _runtime_vars, restrict_to_workspace

### READ_ONLY（允许 get，禁止 set）
workspace, config, iteration, tool_count

### 敏感字段名过滤
api_key, secret, password, token, credential, private_key, access_token

## 3. 配置

`MyToolConfig`：
- `enable: bool = True`
- `allow_set: bool = False`

## 4. 工具发现契约

`ToolLoader` 扫描 `tools/self.py` 时发现 `MyTool`。
最终注册：`my`
