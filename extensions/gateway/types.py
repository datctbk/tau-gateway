"""Gateway type definitions.

Data classes for messages, send results, and session source identification
used throughout the gateway system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MessageType(str, Enum):
    """Types of inbound messages."""
    TEXT = "text"
    PHOTO = "photo"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    COMMAND = "command"
    STICKER = "sticker"
    VIDEO = "video"
    LOCATION = "location"


# ---------------------------------------------------------------------------
# Session Source
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionSource:
    """Identifies where a message came from — used as session key components.

    Immutable (frozen) so it can be used as a dict key / in sets.
    """
    platform: str
    chat_id: str
    chat_name: str = ""
    chat_type: str = ""       # "private", "group", "supergroup", "channel", "dm"
    user_id: str = ""
    user_name: str = ""
    thread_id: str | None = None

    def session_key(self) -> str:
        """Deterministic key for session lookup."""
        parts = [self.platform, self.chat_id]
        if self.thread_id:
            parts.append(self.thread_id)
        return ":".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionSource":
        return cls(
            platform=d.get("platform", "unknown"),
            chat_id=str(d.get("chat_id", "")),
            chat_name=d.get("chat_name", ""),
            chat_type=d.get("chat_type", ""),
            user_id=str(d.get("user_id", "")),
            user_name=d.get("user_name", ""),
            thread_id=d.get("thread_id"),
        )

    def display(self) -> str:
        """Human-readable description."""
        name = self.chat_name or self.chat_id
        if self.thread_id:
            return f"{self.platform}:{name}#{self.thread_id}"
        return f"{self.platform}:{name}"


# ---------------------------------------------------------------------------
# Message Event
# ---------------------------------------------------------------------------

@dataclass
class MessageEvent:
    """An inbound message from any platform.

    Normalized representation that all platform adapters convert to.
    """
    text: str
    message_type: MessageType
    source: SessionSource
    timestamp: float = field(default_factory=time.time)
    message_id: str = ""

    # Media
    media_urls: List[str] = field(default_factory=list)
    media_paths: List[str] = field(default_factory=list)  # locally cached

    # Reply context
    reply_to_message_id: str | None = None
    reply_to_text: str | None = None

    # Raw platform data (for adapter-specific processing)
    raw: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "message_type": self.message_type.value,
            "source": self.source.to_dict(),
            "timestamp": self.timestamp,
            "message_id": self.message_id,
            "media_urls": self.media_urls,
            "media_paths": self.media_paths,
            "reply_to_message_id": self.reply_to_message_id,
            "reply_to_text": self.reply_to_text,
        }


# ---------------------------------------------------------------------------
# Send Result
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    """Result of sending a message through a platform adapter."""
    success: bool
    message_id: str = ""
    error: str = ""
    retryable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message_id": self.message_id,
            "error": self.error,
            "retryable": self.retryable,
        }


# ---------------------------------------------------------------------------
# Delivery Target
# ---------------------------------------------------------------------------

@dataclass
class DeliveryTarget:
    """Where to send a response message."""
    platform: str
    chat_id: str
    thread_id: str | None = None
    is_origin: bool = False    # True if this is where the message came from

    @property
    def key(self) -> str:
        parts = [self.platform, self.chat_id]
        if self.thread_id:
            parts.append(self.thread_id)
        return ":".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "is_origin": self.is_origin,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeliveryTarget":
        return cls(
            platform=d.get("platform", ""),
            chat_id=str(d.get("chat_id", "")),
            thread_id=d.get("thread_id"),
            is_origin=d.get("is_origin", False),
        )

    @classmethod
    def from_source(cls, source: SessionSource) -> "DeliveryTarget":
        """Create a delivery target from a session source (origin)."""
        return cls(
            platform=source.platform,
            chat_id=source.chat_id,
            thread_id=source.thread_id,
            is_origin=True,
        )

    @classmethod
    def parse(cls, spec: str) -> "DeliveryTarget":
        """Parse a target string like 'telegram:12345' or 'discord:guild#thread'."""
        parts = spec.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid target spec: {spec!r} — expected 'platform:chat_id'")
        platform = parts[0].strip()
        rest = parts[1].strip()
        thread_id = None
        if "#" in rest:
            chat_id, thread_id = rest.rsplit("#", 1)
        else:
            chat_id = rest
        return cls(platform=platform, chat_id=chat_id, thread_id=thread_id)
