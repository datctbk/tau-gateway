"""Channel directory — enumerate and resolve messaging targets.

Provides fuzzy name resolution for human-friendly channel references
(e.g. "dev-team" → discord:123456789) and formatted display listings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .types import SessionSource

logger = logging.getLogger(__name__)


@dataclass
class ChannelInfo:
    """Metadata about a reachable channel on a connected platform."""
    platform: str
    chat_id: str
    name: str
    chat_type: str = ""
    member_count: int = 0

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.chat_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "name": self.name,
            "type": self.chat_type,
            "member_count": self.member_count,
        }


class ChannelDirectory:
    """Cached directory of reachable channels across platforms."""

    def __init__(self):
        self._channels: Dict[str, ChannelInfo] = {}
        self._session_sources: Dict[str, SessionSource] = {}

    def add_channel(self, channel: ChannelInfo) -> None:
        """Add or update a channel entry."""
        self._channels[channel.key] = channel

    def add_from_source(self, source: SessionSource) -> None:
        """Record a channel from an inbound message source."""
        key = f"{source.platform}:{source.chat_id}"
        self._session_sources[key] = source
        if key not in self._channels:
            self._channels[key] = ChannelInfo(
                platform=source.platform,
                chat_id=source.chat_id,
                name=source.chat_name or source.chat_id,
                chat_type=source.chat_type,
            )

    async def refresh_from_adapters(self, adapters: Dict[str, Any]) -> None:
        """Enumerate channels from all connected platform adapters."""
        for platform_name, adapter in adapters.items():
            try:
                channels = await adapter.list_channels()
                for ch in channels:
                    self.add_channel(ChannelInfo(
                        platform=platform_name,
                        chat_id=str(ch.get("chat_id", "")),
                        name=ch.get("name", ""),
                        chat_type=ch.get("type", ""),
                        member_count=ch.get("member_count", 0),
                    ))
            except Exception as exc:
                logger.warning("Failed to list channels for %s: %s", platform_name, exc)

    def resolve(self, query: str) -> Optional[ChannelInfo]:
        """Resolve a channel reference to a ChannelInfo.

        Supports:
        - Exact key: "telegram:12345"
        - Name match: "dev-team" (fuzzy)
        - Platform-scoped name: "discord:dev-team"
        """
        # Exact key match
        if query in self._channels:
            return self._channels[query]

        # Check if it's a platform:name pattern
        platform_filter = None
        search_name = query
        if ":" in query:
            parts = query.split(":", 1)
            platform_filter = parts[0].lower()
            search_name = parts[1]

        # Fuzzy name matching
        query_lower = search_name.lower()
        candidates: list[ChannelInfo] = []

        for ch in self._channels.values():
            if platform_filter and ch.platform != platform_filter:
                continue

            name_lower = ch.name.lower()
            if name_lower == query_lower:
                return ch
            if name_lower.startswith(query_lower):
                candidates.append(ch)
            elif query_lower in name_lower:
                candidates.append(ch)

        if len(candidates) == 1:
            return candidates[0]

        return None

    def list_all(self, platform: str | None = None) -> List[ChannelInfo]:
        """List all known channels, optionally filtered by platform."""
        channels = list(self._channels.values())
        if platform:
            channels = [ch for ch in channels if ch.platform == platform]
        return sorted(channels, key=lambda ch: (ch.platform, ch.name))

    def format_for_display(self, platform: str | None = None) -> str:
        """Format the channel directory for display to the user or model."""
        channels = self.list_all(platform)
        if not channels:
            return "No channels available."

        lines = ["**Available channels:**", ""]
        current_platform = ""
        for ch in channels:
            if ch.platform != current_platform:
                current_platform = ch.platform
                lines.append(f"  **{current_platform}:**")
            type_info = f" ({ch.chat_type})" if ch.chat_type else ""
            lines.append(f"    - {ch.name}{type_info} → `{ch.key}`")

        return "\n".join(lines)

    def format_for_model(self, platform: str | None = None) -> str:
        """Format as structured text for the LLM context window."""
        channels = self.list_all(platform)
        if not channels:
            return "No messaging channels available."

        lines = ["Available messaging channels:"]
        for ch in channels:
            lines.append(f"  {ch.key} — {ch.name} ({ch.chat_type or 'unknown'})")

        return "\n".join(lines)
