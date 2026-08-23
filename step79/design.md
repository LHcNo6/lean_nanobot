# Step 79 Design: ImageGenerationTool

## 1. 架构

```
tools/image_generation.py（新建）
  ├── ImageGenerationConfig      配置
  ├── ImageGenerationProvider    provider 抽象基类
  ├── SimpleSvgProvider          默认占位 provider（生成 SVG）
  └── ImageGenerationTool(Tool)  图片生成工具
```

## 2. 参数

```python
prompt: str                    # 必填，图片描述
reference_images: list[str]    # 可选，参考图片路径
aspect_ratio: str = "1:1"      # 可选，宽高比
image_size: str = "1K"         # 可选，尺寸提示
count: int = 1                 # 可选，生成数量（1-8）
```

## 3. Provider 抽象

```python
class ImageGenerationProvider(ABC):
    async def generate(self, prompt, aspect_ratio, image_size) -> bytes
```

SimpleSvgProvider：生成一个包含 prompt 文本的 SVG 占位图。

## 4. 执行流程

1. 校验 prompt 非空
2. 校验 count 在 1-8 范围内
3. 创建 generated 目录
4. 调用 provider.generate() 生成图片
5. 保存到 generated/{timestamp}_{index}.svg
6. 返回图片路径列表

## 5. 测试策略

- 生成单张图片
- 生成多张图片（count）
- aspect_ratio 影响尺寸
- 缺少 prompt 报错
- count 超出范围报错
- 工具发现
