# Step 79 API Specification

## 1. ImageGenerationTool API

**文件**：`tools/image_generation.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"generate_image"` |
| `config_key` | `"image_generation"` |
| `_scopes` | `{"core"}` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | string | 是 | — | 图片描述 |
| `reference_images` | array[string] | 否 | `[]` | 参考图片路径 |
| `aspect_ratio` | string | 否 | `"1:1"` | 宽高比（1:1, 16:9, 9:16, 4:3） |
| `image_size` | string | 否 | `"1K"` | 尺寸提示 |
| `count` | integer | 否 | `1` | 生成数量（1-8） |

### 返回值

成功：文本（生成的图片路径列表）
失败：`ToolResult.error(...)`

## 2. 配置

`ImageGenerationConfig`：
- `enabled: bool = False`
- `provider: str = "simple"`
- `save_dir: str = "generated"`

## 3. 工具发现契约

`ToolLoader` 扫描 `tools/image_generation.py` 时发现 `ImageGenerationTool`。
最终注册：`generate_image`
