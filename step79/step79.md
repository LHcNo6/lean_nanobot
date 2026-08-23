# step79：ImageGenerationTool

## 实现

新建 `tools/image_generation.py`：
- ImageGenerationProvider ABC + SimpleSvgProvider 占位实现
- 支持 prompt/reference_images/aspect_ratio/image_size/count
- 保存到 workspace/generated 目录
- 配置：ImageGenerationConfig（enabled/provider/save_dir）

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `config/schema.py` | 修改：+ImageGenerationConfig + ToolsConfig.image_generation |
| `tools/image_generation.py` | 新建 |
| `tests/test_image_generation.py` | 新建（9测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

9 passed
