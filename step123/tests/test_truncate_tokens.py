"""step123: truncate_text_to_tokens 测试。"""

from __future__ import annotations

from step123.helpers import truncate_text, truncate_text_to_tokens


class TestTruncateTextToTokens:
    """truncate_text_to_tokens 函数测试。"""

    def test_zero_max_tokens_returns_original(self) -> None:
        """max_tokens <= 0 时返回原文。"""
        text = "hello world"
        assert truncate_text_to_tokens(text, 0) == text
        assert truncate_text_to_tokens(text, -5) == text

    def test_short_text_unchanged(self) -> None:
        """短文本不超过预算时返回原文。"""
        text = "hello"
        assert truncate_text_to_tokens(text, 100) == text

    def test_long_text_truncated(self) -> None:
        """长文本被截断并带后缀。"""
        text = "word " * 500  # 约 500+ tokens
        result = truncate_text_to_tokens(text, 50)
        assert len(result) < len(text)
        assert result.endswith("... (truncated)")

    def test_chinese_text(self) -> None:
        """中文文本按 token 截断（中文字符 token 效率不同）。"""
        text = "这是一段中文测试内容。" * 100
        result = truncate_text_to_tokens(text, 30)
        assert result.endswith("... (truncated)")
        # 中文 30 tokens 应该远少于 30*4=120 字符
        assert len(result) < 200

    def test_empty_string(self) -> None:
        """空字符串返回空字符串。"""
        assert truncate_text_to_tokens("", 10) == ""

    def test_fallback_mode(self, monkeypatch) -> None:
        """tiktoken 不可用时回退到 char 估算。"""
        import step123.helpers as helpers

        def mock_get_encoding():
            raise ImportError("tiktoken not available")

        monkeypatch.setattr(helpers, "_get_token_encoding", mock_get_encoding)
        text = "a" * 1000
        result = truncate_text_to_tokens(text, 10)  # 10 tokens ≈ 40 chars
        assert result.endswith("... (truncated)")
        assert len(result) < len(text)

    def test_consistency_with_truncate_text(self) -> None:
        """回退模式下与 truncate_text 行为一致。"""
        import step123.helpers as helpers

        def mock_get_encoding():
            raise ImportError("tiktoken not available")

        monkeypatch = None  # 手动模拟
        text = "b" * 500
        # 直接测试回退逻辑：max_tokens=10 → max_chars=40 → 40-15=25 字符 + 后缀
        original = helpers._get_token_encoding
        helpers._get_token_encoding = lambda: (_ for _ in ()).throw(ImportError())
        try:
            result = truncate_text_to_tokens(text, 10)
            expected = truncate_text(text, 25)  # 40 - 15(后缀) = 25
            assert result == expected
        finally:
            helpers._get_token_encoding = original
