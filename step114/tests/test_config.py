"""Step 25 — Pydantic 配置系统（H1）测试。

全部使用构造数据 / mock 环境变量，不依赖真实 API Key 或网络。
覆盖：schema（默认值/双写别名/预设校验/provider 匹配）、
loader（文件加载/迁移/env 补缺/`${VAR}` 解析/save 往返）、
factory Config 路径（装配/回退/签名/快照）、AgentLoop.from_config、
Tool.config_cls() 落地。
"""

from __future__ import annotations

import json

import pytest

from step114.config.loader import (
    _env_to_config_dict,
    get_config_path,
    load_config,
    resolve_config_env_vars,
    save_config,
    set_config_path,
)
from step114.config.schema import Config, ProviderConfig
from step114.context import ToolContext
from step114.loop import AgentLoop
from step114.openai_compat_provider import OpenAICompatProvider
from step114.providers.factory import (
    ProviderSettings,
    build_provider_snapshot,
    is_config_input,
    make_provider,
    provider_signature,
)
from step114.providers.fallback_provider import FallbackProvider
from step114.tools.echo import EchoTool, EchoToolConfig


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_defaults(self):
        config = Config()
        d = config.agents.defaults
        assert d.model == "gpt-4o-mini"
        assert d.provider == "auto"
        assert d.max_tokens == 8192
        assert d.context_window_tokens == 200_000
        assert d.max_tool_result_chars == 16_000
        assert d.session_ttl_minutes == 0
        assert d.consolidation_ratio == 0.5
        assert d.disabled_skills == []
        assert d.bot_name == "lean_nanobot"
        assert config.resolve_preset().model == d.model

    def test_camel_and_snake_aliases(self):
        camel = Config.model_validate({"agents": {"defaults": {"maxTokens": 100, "sessionTtlMinutes": 30}}})
        snake = Config.model_validate({"agents": {"defaults": {"max_tokens": 200, "session_ttl_minutes": 45}}})
        assert camel.agents.defaults.max_tokens == 100
        assert camel.agents.defaults.session_ttl_minutes == 30
        assert snake.agents.defaults.max_tokens == 200
        assert snake.agents.defaults.session_ttl_minutes == 45

    def test_serialization_by_alias_camel(self):
        config = Config.model_validate({"agents": {"defaults": {"sessionTtlMinutes": 30}}})
        dumped = config.model_dump(mode="json", by_alias=True)
        assert dumped["agents"]["defaults"]["sessionTtlMinutes"] == 30
        assert "modelPresets" in dumped

    def test_model_preset_resolution(self):
        config = Config.model_validate({
            "modelPresets": {"fast": {"model": "gpt-4o", "maxTokens": 4096}},
            "agents": {"defaults": {"modelPreset": "fast"}},
        })
        assert config.resolve_preset("fast").model == "gpt-4o"
        assert config.resolve_preset().model == "gpt-4o"
        assert config.resolve_preset("default").max_tokens == 8192
        with pytest.raises(KeyError):
            config.resolve_preset("nope")

    def test_model_preset_validation(self):
        with pytest.raises(ValueError, match="not found"):
            Config.model_validate({"agents": {"defaults": {"modelPreset": "ghost"}}})
        with pytest.raises(ValueError, match="reserved"):
            Config.model_validate({"modelPresets": {"default": {"model": "x"}}})

    def test_provider_matching_by_keyword(self):
        config = Config.model_validate({
            "providers": {
                "openai": {"apiKey": "sk-openai"},
                "deepseek": {"apiKey": "sk-deepseek"},
            }
        })
        assert config.get_provider("gpt-4o").api_key == "sk-openai"
        assert config.get_provider_name("gpt-4o") == "openai"
        assert config.get_provider("deepseek-chat").api_key == "sk-deepseek"
        assert config.get_provider("no-such-model") is None
        assert config.get_api_key("gpt-4o") == "sk-openai"

    def test_provider_matching_by_forced_name(self):
        config = Config.model_validate({
            "agents": {"defaults": {"provider": "ollama"}},
        })
        assert config.get_provider_name("anything") == "ollama"
        assert config.get_provider("anything").api_key is None

    def test_get_api_base_falls_back_to_spec_default(self):
        config = Config.model_validate({"providers": {"deepseek": {"apiKey": "k"}}})
        assert config.get_api_base("deepseek-chat") == "https://api.deepseek.com"

    def test_custom_provider_in_extra(self):
        config = Config.model_validate({
            "providers": {"my-gateway": {"apiBase": "https://gw.example/v1", "apiKey": "k"}},
            "agents": {"defaults": {"provider": "my-gateway"}},
        })
        p = config.get_provider("anything")
        assert isinstance(p, ProviderConfig)
        assert p.api_base == "https://gw.example/v1"
        assert config.get_provider_name("anything") == "my-gateway"

    def test_channels_extra_sections(self):
        config = Config.model_validate({
            "channels": {
                "cli": {"enabled": False, "allow_from": ["*"]},
                "sendProgress": False,
            }
        })
        sections = config.channels.channel_sections()
        assert sections["cli"] == {"enabled": False, "allow_from": ["*"]}
        assert config.channels.send_progress is False

    def test_workspace_path(self):
        from pathlib import Path

        config = Config.model_validate({"agents": {"defaults": {"workspace": "~/ws"}}})
        assert config.workspace_path == Path("~/ws").expanduser()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_load_missing_file_returns_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.json")
        assert config.agents.defaults.model == "gpt-4o-mini"

    def test_load_json_file_with_camel_keys(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"agents": {"defaults": {"maxTokens": 1234}}}), encoding="utf-8"
        )
        assert load_config(path).agents.defaults.max_tokens == 1234

    def test_load_bad_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to load config"):
            load_config(path)

    def test_migrate_legacy_max_messages(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"agents": {"defaults": {"maxMessages": 25, "maxTokens": 1234}}}),
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.agents.defaults.max_tokens == 1234
        assert not hasattr(config.agents.defaults, "max_messages")

    def test_env_fills_missing_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NANOBOT_AGENTS__DEFAULTS__MAX_TOKENS", "999")
        config = load_config(tmp_path / "nonexistent.json")
        assert config.agents.defaults.max_tokens == 999

    def test_env_file_wins_over_env(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"agents": {"defaults": {"maxTokens": 123}}}), encoding="utf-8")
        monkeypatch.setenv("NANOBOT_AGENTS__DEFAULTS__MAX_TOKENS", "999")
        assert load_config(path).agents.defaults.max_tokens == 123

    def test_env_value_coercion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NANOBOT_AGENTS__DEFAULTS__SESSION_TTL_MINUTES", "7")
        monkeypatch.setenv("NANOBOT_AGENTS__DEFAULTS__FAIL_ON_TOOL_ERROR", "false")
        config = load_config(tmp_path / "nonexistent.json")
        assert config.agents.defaults.session_ttl_minutes == 7
        assert config.agents.defaults.fail_on_tool_error is False

    def test_env_nested_model_presets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NANOBOT_MODEL_PRESETS__FAST__MODEL", "gpt-4o")
        config = load_config(tmp_path / "nonexistent.json")
        assert config.model_presets["fast"].model == "gpt-4o"

    def test_env_to_config_dict_ignores_foreign_vars(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_X", "1")
        monkeypatch.setenv("OTHER_VAR", "2")
        assert _env_to_config_dict() == {"x": "1"}

    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "config.json"
        config = Config.model_validate({"agents": {"defaults": {"maxTokens": 777}}})
        save_config(config, path)
        reloaded = load_config(path)
        assert reloaded.agents.defaults.max_tokens == 777
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["agents"]["defaults"]["maxTokens"] == 777

    def test_save_preserves_env_templates(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"providers": {"deepseek": {"apiKey": "${DS_KEY}"}}}),
            encoding="utf-8",
        )
        raw = load_config(path)
        save_config(raw, path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["providers"]["deepseek"]["apiKey"] == "${DS_KEY}"

    def test_resolve_env_vars_and_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DS_KEY", "real-key")
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"providers": {"deepseek": {"apiKey": "${DS_KEY}"}}}),
            encoding="utf-8",
        )
        raw = load_config(path)
        assert raw.providers.deepseek.api_key == "${DS_KEY}"
        resolved = resolve_config_env_vars(raw)
        assert resolved.providers.deepseek.api_key == "real-key"
        monkeypatch.delenv("DS_KEY")
        with pytest.raises(ValueError, match="DS_KEY"):
            resolve_config_env_vars(raw)

    def test_set_get_config_path(self):
        set_config_path("tmp-test-path.json")
        try:
            assert str(get_config_path()) == "tmp-test-path.json"
        finally:
            set_config_path(None)


# ---------------------------------------------------------------------------
# Factory — Config 路径
# ---------------------------------------------------------------------------


class TestFactoryFromConfig:
    def test_make_provider_openai(self):
        config = Config.model_validate({"providers": {"openai": {"apiKey": "sk-test"}}})
        provider = make_provider(config)
        assert isinstance(provider, OpenAICompatProvider)
        assert provider._client.api_key == "sk-test"
        assert "api.openai.com" in str(provider._client.base_url)
        assert provider.model == "gpt-4o-mini"

    def test_make_provider_local_requires_no_key(self):
        config = Config.model_validate({
            "modelPresets": {"local": {"model": "llama3", "provider": "ollama"}},
            "agents": {"defaults": {"modelPreset": "local"}},
        })
        provider = make_provider(config)
        assert isinstance(provider, OpenAICompatProvider)
        assert provider._client.api_key == "missing"

    def test_make_provider_custom_requires_api_base(self):
        config = Config.model_validate({
            "providers": {"custom": {"apiKey": "k"}},
            "agents": {"defaults": {"model": "x", "provider": "custom"}},
        })
        with pytest.raises(ValueError, match="api_base"):
            make_provider(config)

    def test_make_provider_missing_key_raises(self):
        config = Config.model_validate({
            "agents": {"defaults": {"model": "deepseek-chat", "provider": "deepseek"}},
        })
        with pytest.raises(ValueError, match="No API key"):
            make_provider(config)

    def test_make_provider_env_key_fallback(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        config = Config.model_validate({
            "agents": {"defaults": {"model": "deepseek-chat", "provider": "deepseek"}},
        })
        provider = make_provider(config)
        assert provider._client.api_key == "env-key"

    def test_make_provider_fallback_models_wrap(self):
        config = Config.model_validate({
            "providers": {"openai": {"apiKey": "sk-a"}},
            "agents": {"defaults": {
                "model": "gpt-4o",
                "fallbackModels": ["deepseek-chat"],
            }},
        })
        provider = make_provider(config)
        assert isinstance(provider, FallbackProvider)
        assert len(provider._fallback_presets) == 1

    def test_provider_signature_reflects_config(self):
        base = {"agents": {"defaults": {"model": "gpt-4o"}}}
        s1 = provider_signature(Config.model_validate(
            {**base, "providers": {"openai": {"apiKey": "a"}}}
        ))
        s2 = provider_signature(Config.model_validate(
            {**base, "providers": {"openai": {"apiKey": "b"}}}
        ))
        assert s1 != s2

    def test_build_provider_snapshot(self):
        config = Config.model_validate({
            "providers": {"openai": {"apiKey": "sk-a"}},
            "agents": {"defaults": {
                "model": "gpt-4o",
                "maxTokens": 1024,
                "contextWindowTokens": 8192,
                "fallbackModels": ["deepseek-chat"],
            }},
        })
        snap = build_provider_snapshot(config)
        assert snap.model == "gpt-4o"
        assert snap.context_window_tokens == 8192
        assert snap.generation.max_tokens == 1024
        assert isinstance(snap.provider, FallbackProvider)

    def test_dual_dispatch_legacy_settings_still_works(self):
        assert not is_config_input(ProviderSettings(model="gpt-4o"))
        assert is_config_input(Config())
        provider = make_provider(ProviderSettings(model="gpt-4o", api_key="sk-test"))
        assert isinstance(provider, OpenAICompatProvider)


# ---------------------------------------------------------------------------
# AgentLoop.from_config + Tool.config_cls
# ---------------------------------------------------------------------------


class TestFromConfig:
    def _config(self, tmp_path):
        return Config.model_validate({
            "providers": {"openai": {"apiKey": "sk-test"}},
            "agents": {"defaults": {
                "workspace": str(tmp_path / "ws"),
                "model": "gpt-4o",
                "maxTokens": 512,
                "contextWindowTokens": 4096,
                "sessionTtlMinutes": 5,
                "maxToolResultChars": 777,
            }},
        })

    def test_from_config_wiring(self, tmp_path):
        from step114.bus import MessageBus

        config = self._config(tmp_path)
        loop = AgentLoop.from_config(config, bus=MessageBus())
        assert loop.config is config
        assert loop.runtime.model == "gpt-4o"
        assert loop.runtime.context_window_tokens == 4096
        assert loop.runtime.max_tokens == 512
        assert loop.max_tool_result_chars == 777
        assert loop.replay_budget == 4096 - 512 - 128
        assert loop.auto_compact._ttl == 5
        assert loop.sessions.sessions_dir == tmp_path / "ws" / "sessions"
        assert "lean_nanobot" in loop.identity

    def test_from_config_extra_overrides(self, tmp_path):
        from step114.bus import MessageBus

        config = self._config(tmp_path)
        identity = "custom identity"
        loop = AgentLoop.from_config(
            config, bus=MessageBus(), identity=identity,
            session_ttl_minutes=9,
        )
        assert loop.identity == identity
        assert loop.auto_compact._ttl == 9


class TestToolConfigCls:
    def _tool_ctx(self, config: Config) -> ToolContext:
        return ToolContext(config=config, workspace=".")

    def test_echo_defaults_when_no_section(self):
        config = Config()
        tool = EchoTool.create(self._tool_ctx(config))
        assert tool.tool_config.enabled is True
        assert tool.tool_config.prefix == ""
        assert asyncio_run(tool.execute(text="hi")) == "Echo: hi"

    def test_echo_config_applied(self):
        config = Config.model_validate({
            "tools": {"echo": {"enabled": False, "prefix": "> ", "maxLength": 5}},
        })
        ctx = self._tool_ctx(config)
        assert EchoTool.enabled(ctx) is False
        tool = EchoTool.create(ctx)
        assert tool.tool_config == EchoToolConfig(enabled=False, prefix="> ", max_length=5)

    def test_echo_execute_applies_config(self):
        config = Config.model_validate({
            "tools": {"echo": {"prefix": ">> ", "maxLength": 5}},
        })
        tool = EchoTool.create(self._tool_ctx(config))
        result = asyncio_run(tool.execute(text="hello world"))
        assert result == "Echo: >> he"

    def test_echo_create_without_ctx(self):
        tool = EchoTool.create(None)
        assert isinstance(tool, EchoTool)
        assert tool.tool_config.prefix == ""

    def test_echo_legacy_direct_construct(self):
        tool = EchoTool()
        assert asyncio_run(tool.execute(text="x")) == "Echo: x"


def asyncio_run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
