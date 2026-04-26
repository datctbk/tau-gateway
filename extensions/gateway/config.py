"""Gateway configuration management.

Loads platform credentials, session reset policies, and delivery settings
from ~/.tau/gateway.yaml with environment variable overrides.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TAU_HOME = Path.home() / ".tau"
DEFAULT_CONFIG_PATH = TAU_HOME / "gateway.yaml"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Platform(str, Enum):
    """Supported messaging platforms."""
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    API_SERVER = "api_server"
    WEBHOOK = "webhook"
    EMAIL = "email"
    SMS = "sms"

    @classmethod
    def from_str(cls, value: str) -> "Platform":
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(
                f"Unknown platform: {value!r}. "
                f"Supported: {', '.join(p.value for p in cls)}"
            )


class ResetMode(str, Enum):
    """Session reset policy modes."""
    NONE = "none"
    DAILY = "daily"
    IDLE = "idle"
    BOTH = "both"


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------

@dataclass
class HomeChannel:
    """A home channel where the agent can proactively post."""
    platform: Platform
    chat_id: str
    chat_name: str = ""
    thread_id: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "thread_id": self.thread_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HomeChannel":
        return cls(
            platform=Platform.from_str(d["platform"]),
            chat_id=str(d["chat_id"]),
            chat_name=d.get("chat_name", ""),
            thread_id=d.get("thread_id"),
        )


@dataclass
class SessionResetPolicy:
    """When to automatically reset (clear) a session."""
    mode: ResetMode = ResetMode.NONE
    at_hour: int = 4          # Hour of day (UTC) for daily reset
    idle_minutes: int = 120   # Minutes of inactivity for idle reset

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "at_hour": self.at_hour,
            "idle_minutes": self.idle_minutes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionResetPolicy":
        return cls(
            mode=ResetMode(d.get("mode", "none")),
            at_hour=int(d.get("at_hour", 4)),
            idle_minutes=int(d.get("idle_minutes", 120)),
        )


@dataclass
class StreamingConfig:
    """Streaming response configuration."""
    enabled: bool = True
    edit_interval_ms: int = 800      # How often to edit-in-place (ms)
    min_chunk_chars: int = 20        # Minimum chars before sending an edit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "edit_interval_ms": self.edit_interval_ms,
            "min_chunk_chars": self.min_chunk_chars,
        }


@dataclass
class PlatformConfig:
    """Configuration for a single platform adapter."""
    platform: Platform
    enabled: bool = False
    token: str = ""
    api_key: str = ""
    home_channels: list[HomeChannel] = field(default_factory=list)
    reply_to_mode: str = "always"    # always | mention | never
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform.value,
            "enabled": self.enabled,
            "token": self.token,
            "api_key": self.api_key,
            "home_channels": [hc.to_dict() for hc in self.home_channels],
            "reply_to_mode": self.reply_to_mode,
            "streaming": self.streaming.to_dict(),
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlatformConfig":
        platform = Platform.from_str(d.get("platform", "local"))
        home_channels = [
            HomeChannel.from_dict(hc) for hc in d.get("home_channels", [])
        ]
        streaming = StreamingConfig(**d["streaming"]) if "streaming" in d else StreamingConfig()
        return cls(
            platform=platform,
            enabled=d.get("enabled", False),
            token=d.get("token", ""),
            api_key=d.get("api_key", ""),
            home_channels=home_channels,
            reply_to_mode=d.get("reply_to_mode", "always"),
            streaming=streaming,
            extra=d.get("extra", {}),
        )


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""
    platforms: Dict[str, PlatformConfig] = field(default_factory=dict)
    reset_policy: SessionResetPolicy = field(default_factory=SessionResetPolicy)
    provider: str = "openai"
    model: str = "gpt-4o"
    max_tokens: int = 8192
    max_turns: int = 20
    system_prompt: str = ""

    def enabled_platforms(self) -> list[PlatformConfig]:
        """Return list of enabled platform configs."""
        return [pc for pc in self.platforms.values() if pc.enabled]

    def get_platform(self, platform: Platform | str) -> Optional[PlatformConfig]:
        """Get config for a specific platform."""
        key = platform.value if isinstance(platform, Platform) else platform
        return self.platforms.get(key)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": {k: v.to_dict() for k, v in self.platforms.items()},
            "reset_policy": self.reset_policy.to_dict(),
            "provider": self.provider,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "max_turns": self.max_turns,
            "system_prompt": self.system_prompt,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _env_override(platform: str, key: str, default: str = "") -> str:
    """Check for TAU_GATEWAY_{PLATFORM}_{KEY} environment variable."""
    env_key = f"TAU_GATEWAY_{platform.upper()}_{key.upper()}"
    return os.environ.get(env_key, default)


def load_gateway_config(config_path: Path | None = None) -> GatewayConfig:
    """Load gateway configuration from YAML file + environment overrides.

    Configuration file: ~/.tau/gateway.yaml (or *config_path*).

    Environment variable overrides:
        TAU_GATEWAY_TELEGRAM_TOKEN
        TAU_GATEWAY_DISCORD_TOKEN
        TAU_GATEWAY_SLACK_TOKEN
        TAU_GATEWAY_API_SERVER_API_KEY
        TAU_GATEWAY_PROVIDER
        TAU_GATEWAY_MODEL
    """
    path = config_path or DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}

    if path.is_file():
        try:
            import yaml
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            logger.info("Loaded gateway config from %s", path)
        except ImportError:
            # Fall back to JSON-subset parsing if PyYAML not installed
            try:
                import json
                raw = json.loads(path.read_text(encoding="utf-8"))
                logger.info("Loaded gateway config (JSON) from %s", path)
            except Exception as e:
                logger.warning("Failed to parse gateway config %s: %s", path, e)
        except Exception as e:
            logger.warning("Failed to load gateway config %s: %s", path, e)
    else:
        logger.debug("No gateway config file at %s — using defaults", path)

    # Parse platform configs
    platforms: Dict[str, PlatformConfig] = {}
    for plat_name, plat_data in raw.get("platforms", {}).items():
        if isinstance(plat_data, dict):
            plat_data.setdefault("platform", plat_name)
            platforms[plat_name] = PlatformConfig.from_dict(plat_data)

    # Environment variable overrides for platform tokens
    env_mappings = {
        "telegram": "TOKEN",
        "discord": "TOKEN",
        "slack": "TOKEN",
        "api_server": "API_KEY",
    }
    for plat_name, env_key in env_mappings.items():
        env_val = _env_override(plat_name, env_key)
        if env_val:
            if plat_name not in platforms:
                platforms[plat_name] = PlatformConfig(
                    platform=Platform.from_str(plat_name),
                    enabled=True,
                )
            pc = platforms[plat_name]
            if env_key == "TOKEN":
                pc.token = env_val
            elif env_key == "API_KEY":
                pc.api_key = env_val
            pc.enabled = True

    # Reset policy
    reset_data = raw.get("reset_policy", {})
    reset_policy = SessionResetPolicy.from_dict(reset_data) if reset_data else SessionResetPolicy()

    # Global env overrides
    provider = os.environ.get("TAU_GATEWAY_PROVIDER", raw.get("provider", "openai"))
    model = os.environ.get("TAU_GATEWAY_MODEL", raw.get("model", "gpt-4o"))

    return GatewayConfig(
        platforms=platforms,
        reset_policy=reset_policy,
        provider=provider,
        model=model,
        max_tokens=int(raw.get("max_tokens", 8192)),
        max_turns=int(raw.get("max_turns", 20)),
        system_prompt=raw.get("system_prompt", ""),
    )
