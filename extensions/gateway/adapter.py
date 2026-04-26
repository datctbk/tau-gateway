"""Base platform adapter interface.

All platform adapters (Telegram, Discord, Slack, API Server) inherit from
``BasePlatformAdapter`` and implement the abstract lifecycle + messaging
methods. The adapter handles:
- Connection lifecycle (start/stop)
- Inbound message routing (via message handler callback)
- Outbound message sending with platform-aware chunking
- Media caching (download remote media to local cache)
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

from .config import PlatformConfig, Platform
from .types import MessageEvent, SendResult, SessionSource

logger = logging.getLogger(__name__)

TAU_HOME = Path.home() / ".tau"
MEDIA_CACHE_DIR = TAU_HOME / "cache" / "media"

# Type for the inbound message callback
MessageHandler = Callable[[MessageEvent], Awaitable[None]]


class BasePlatformAdapter(ABC):
    """Abstract base for all platform adapters.

    Subclasses implement platform-specific networking. The gateway runner
    calls ``start()`` / ``stop()`` for lifecycle and ``send()`` for outbound.

    The adapter calls ``self._message_handler(event)`` when a message arrives.
    """

    # Platform-specific message length limits (override in subclass)
    MAX_MESSAGE_LENGTH: int = 4096
    PLATFORM: Platform = Platform.LOCAL

    def __init__(self, config: PlatformConfig):
        self.config = config
        self._message_handler: Optional[MessageHandler] = None
        self._running = False
        self._start_time: float | None = None

    # ── Lifecycle ──

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform and start receiving messages."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect and clean up resources."""

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    # ── Message handling ──

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register the callback for inbound messages."""
        self._message_handler = handler

    async def _dispatch_message(self, event: MessageEvent) -> None:
        """Forward an inbound message to the registered handler."""
        if self._message_handler is None:
            logger.warning(
                "[%s] No message handler registered — dropping message from %s",
                self.PLATFORM.value,
                event.source.display(),
            )
            return
        try:
            await self._message_handler(event)
        except Exception as exc:
            logger.error(
                "[%s] Message handler raised: %s",
                self.PLATFORM.value,
                exc,
                exc_info=True,
            )

    # ── Outbound messaging ──

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """Send a text message to a chat.

        The adapter handles chunking if *text* exceeds ``MAX_MESSAGE_LENGTH``.
        Returns a ``SendResult`` indicating success or failure.
        """

    async def send_chunked(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        parse_mode: str | None = None,
    ) -> list[SendResult]:
        """Split text into chunks and send each one."""
        chunks = self.split_message(text)
        results = []
        for chunk in chunks:
            result = await self.send(
                chat_id=chat_id,
                text=chunk,
                thread_id=thread_id,
                reply_to=reply_to,
                parse_mode=parse_mode,
            )
            results.append(result)
            if not result.success and not result.retryable:
                break
        return results

    # ── Message splitting ──

    def split_message(self, text: str, max_length: int | None = None) -> list[str]:
        """Split a message into chunks that fit within the platform limit.

        Uses smart splitting — prefers splitting at paragraph, then sentence,
        then word boundaries. Never splits mid-word if avoidable.
        """
        limit = max_length or self.MAX_MESSAGE_LENGTH
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            # Try splitting at double newline (paragraph)
            split_pos = remaining.rfind("\n\n", 0, limit)
            if split_pos > limit * 0.3:
                chunks.append(remaining[:split_pos].rstrip())
                remaining = remaining[split_pos:].lstrip("\n")
                continue

            # Try splitting at single newline
            split_pos = remaining.rfind("\n", 0, limit)
            if split_pos > limit * 0.3:
                chunks.append(remaining[:split_pos].rstrip())
                remaining = remaining[split_pos + 1:]
                continue

            # Try splitting at sentence boundary
            sentence_pattern = re.compile(r"[.!?]\s+", re.DOTALL)
            best_split = -1
            for m in sentence_pattern.finditer(remaining[:limit]):
                best_split = m.end()
            if best_split > limit * 0.3:
                chunks.append(remaining[:best_split].rstrip())
                remaining = remaining[best_split:].lstrip()
                continue

            # Try splitting at word boundary
            split_pos = remaining.rfind(" ", 0, limit)
            if split_pos > limit * 0.3:
                chunks.append(remaining[:split_pos])
                remaining = remaining[split_pos + 1:]
                continue

            # Hard split (last resort)
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]

        return [c for c in chunks if c.strip()]

    # ── Media caching ──

    @staticmethod
    def cache_media(
        url: str,
        data: bytes,
        extension: str | None = None,
    ) -> Path:
        """Cache media data locally. Returns the local file path.

        Uses a content-hash filename to deduplicate downloads.
        """
        MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        content_hash = hashlib.sha256(data).hexdigest()[:16]
        if not extension:
            ext = mimetypes.guess_extension(
                mimetypes.guess_type(url)[0] or "application/octet-stream"
            ) or ".bin"
        else:
            ext = extension if extension.startswith(".") else f".{extension}"

        cached_path = MEDIA_CACHE_DIR / f"{content_hash}{ext}"
        if not cached_path.exists():
            cached_path.write_bytes(data)
            logger.debug("Cached media: %s → %s", url[:60], cached_path)
        return cached_path

    @staticmethod
    async def download_and_cache(url: str) -> Optional[Path]:
        """Download a URL and cache it locally. Returns path or None on error."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return BasePlatformAdapter.cache_media(url, resp.content)
        except ImportError:
            # Fallback to urllib (synchronous)
            try:
                import urllib.request
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                return BasePlatformAdapter.cache_media(url, data)
            except Exception as exc:
                logger.warning("Failed to download media %s: %s", url[:60], exc)
                return None
        except Exception as exc:
            logger.warning("Failed to download media %s: %s", url[:60], exc)
            return None

    # ── Channel listing ──

    async def list_channels(self) -> list[dict[str, Any]]:
        """List available channels/chats on this platform.

        Override in subclass for platforms that support channel enumeration.
        Returns list of dicts with keys: chat_id, name, type.
        """
        return []

    # ── Typing indicator ──

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator. Override in subclass if supported."""
        pass

    # ── Status ──

    def status(self) -> dict[str, Any]:
        """Return adapter status for monitoring."""
        return {
            "platform": self.PLATFORM.value,
            "running": self._running,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "config_enabled": self.config.enabled,
        }
