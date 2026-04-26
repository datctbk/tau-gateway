"""Tests for gateway config module."""

import os
from pathlib import Path

import pytest

from extensions.gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    ResetMode,
    SessionResetPolicy,
    StreamingConfig,
    load_gateway_config,
)


class TestPlatformEnum:
    def test_from_str(self):
        assert Platform.from_str("telegram") == Platform.TELEGRAM
        assert Platform.from_str("DISCORD") == Platform.DISCORD
        assert Platform.from_str("slack") == Platform.SLACK

    def test_from_str_invalid(self):
        with pytest.raises(ValueError, match="Unknown platform"):
            Platform.from_str("whatsapp")


class TestHomeChannel:
    def test_roundtrip(self):
        hc = HomeChannel(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_name="Dev Chat",
            thread_id="99",
        )
        d = hc.to_dict()
        restored = HomeChannel.from_dict(d)
        assert restored.platform == Platform.TELEGRAM
        assert restored.chat_id == "12345"
        assert restored.chat_name == "Dev Chat"
        assert restored.thread_id == "99"


class TestSessionResetPolicy:
    def test_default(self):
        policy = SessionResetPolicy()
        assert policy.mode == ResetMode.NONE
        assert policy.at_hour == 4
        assert policy.idle_minutes == 120

    def test_roundtrip(self):
        policy = SessionResetPolicy(
            mode=ResetMode.BOTH,
            at_hour=6,
            idle_minutes=60,
        )
        d = policy.to_dict()
        restored = SessionResetPolicy.from_dict(d)
        assert restored.mode == ResetMode.BOTH
        assert restored.at_hour == 6
        assert restored.idle_minutes == 60


class TestPlatformConfig:
    def test_defaults(self):
        pc = PlatformConfig(platform=Platform.TELEGRAM)
        assert pc.enabled is False
        assert pc.token == ""
        assert pc.reply_to_mode == "always"

    def test_roundtrip(self):
        pc = PlatformConfig(
            platform=Platform.DISCORD,
            enabled=True,
            token="my-token",
            extra={"guild_id": "123"},
        )
        d = pc.to_dict()
        restored = PlatformConfig.from_dict(d)
        assert restored.platform == Platform.DISCORD
        assert restored.enabled is True
        assert restored.token == "my-token"
        assert restored.extra["guild_id"] == "123"


class TestGatewayConfig:
    def test_enabled_platforms(self):
        cfg = GatewayConfig(platforms={
            "telegram": PlatformConfig(platform=Platform.TELEGRAM, enabled=True),
            "discord": PlatformConfig(platform=Platform.DISCORD, enabled=False),
            "slack": PlatformConfig(platform=Platform.SLACK, enabled=True),
        })
        enabled = cfg.enabled_platforms()
        assert len(enabled) == 2
        platforms = {p.platform for p in enabled}
        assert Platform.TELEGRAM in platforms
        assert Platform.SLACK in platforms

    def test_get_platform(self):
        cfg = GatewayConfig(platforms={
            "telegram": PlatformConfig(platform=Platform.TELEGRAM, token="tok"),
        })
        assert cfg.get_platform("telegram") is not None
        assert cfg.get_platform("discord") is None


class TestLoadConfig:
    def test_load_nonexistent(self, tmp_path):
        """Loading from a nonexistent path returns defaults."""
        cfg = load_gateway_config(tmp_path / "nope.yaml")
        assert isinstance(cfg, GatewayConfig)
        assert len(cfg.platforms) == 0

    def test_env_override_telegram(self, tmp_path, monkeypatch):
        """Environment variable creates and enables platform config."""
        monkeypatch.setenv("TAU_GATEWAY_TELEGRAM_TOKEN", "test-token-123")
        cfg = load_gateway_config(tmp_path / "nope.yaml")
        assert "telegram" in cfg.platforms
        tg = cfg.platforms["telegram"]
        assert tg.token == "test-token-123"
        assert tg.enabled is True

    def test_env_override_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TAU_GATEWAY_MODEL", "claude-3")
        cfg = load_gateway_config(tmp_path / "nope.yaml")
        assert cfg.model == "claude-3"

    def test_load_json_config(self, tmp_path):
        """Config file as JSON (fallback when PyYAML is not installed)."""
        config_file = tmp_path / "gateway.yaml"
        import json
        config_file.write_text(json.dumps({
            "provider": "anthropic",
            "model": "claude-3-opus",
            "platforms": {
                "telegram": {
                    "platform": "telegram",
                    "enabled": True,
                    "token": "file-token",
                }
            },
        }))
        cfg = load_gateway_config(config_file)
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-3-opus"
        assert "telegram" in cfg.platforms
