"""Discord platform adapter.

Uses discord.py to connect tau to Discord servers.
Supports text messages, attachments, threads, and commands.

Install: pip install discord.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ..adapter import BasePlatformAdapter
from ..config import PlatformConfig, Platform
from ..types import MessageEvent, MessageType, SendResult, SessionSource

logger = logging.getLogger(__name__)


class DiscordAdapter(BasePlatformAdapter):
    """Discord adapter using discord.py."""

    MAX_MESSAGE_LENGTH = 2000
    PLATFORM = Platform.DISCORD

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self._client: Any = None
        self._ready_event = asyncio.Event()

    async def start(self) -> None:
        """Connect to Discord and start listening."""
        try:
            import discord
        except ImportError:
            raise ImportError("discord.py is required: pip install discord.py")

        if not self.config.token:
            raise ValueError("Discord token is required. Set TAU_GATEWAY_DISCORD_TOKEN.")

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            logger.info("Discord bot connected as %s", self._client.user)
            self._ready_event.set()

        @self._client.event
        async def on_message(message):
            await self._on_message(message)

        # Start the client in a background task
        asyncio.create_task(self._client.start(self.config.token))
        # Wait for ready with timeout
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("Discord connection timed out")

        self._running = True
        self._start_time = time.time()
        logger.info("Discord adapter started")

    async def stop(self) -> None:
        """Disconnect from Discord."""
        if self._client:
            try:
                await self._client.close()
            except Exception as exc:
                logger.warning("Error stopping Discord adapter: %s", exc)
        self._running = False
        logger.info("Discord adapter stopped")

    async def send(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """Send a message to a Discord channel."""
        if not self._client:
            return SendResult(success=False, error="Adapter not started")

        try:
            target_id = int(thread_id) if thread_id else int(chat_id)
            channel = self._client.get_channel(target_id)
            if channel is None:
                channel = await self._client.fetch_channel(target_id)

            msg = await channel.send(text[:self.MAX_MESSAGE_LENGTH])
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable="rate limit" in str(exc).lower(),
            )

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator in a Discord channel."""
        if self._client:
            try:
                channel = self._client.get_channel(int(chat_id))
                if channel:
                    await channel.typing()
            except Exception:
                pass

    # ── Inbound ──

    async def _on_message(self, message: Any) -> None:
        """Handle a Discord message."""
        # Ignore own messages
        if message.author == self._client.user:
            return

        # Ignore bots
        if message.author.bot:
            return

        # Check reply_to_mode
        if self.config.reply_to_mode == "mention":
            mentioned = any(
                user.id == self._client.user.id
                for user in message.mentions
            )
            if not mentioned and not isinstance(message.channel, getattr(message, "DMChannel", type(None))):
                return

        # Build source
        guild_name = message.guild.name if message.guild else "DM"
        chat_type = "dm" if message.guild is None else "channel"

        thread_id = None
        import discord
        if isinstance(message.channel, discord.Thread):
            thread_id = str(message.channel.id)
            chat_id = str(message.channel.parent_id)
        else:
            chat_id = str(message.channel.id)

        source = SessionSource(
            platform="discord",
            chat_id=chat_id,
            chat_name=f"{guild_name}#{message.channel.name}" if hasattr(message.channel, "name") else guild_name,
            chat_type=chat_type,
            user_id=str(message.author.id),
            user_name=str(message.author),
            thread_id=thread_id,
        )

        # Handle attachments
        media_urls: list[str] = []
        media_paths: list[str] = []
        for attachment in message.attachments:
            media_urls.append(attachment.url)
            cached = await self.download_and_cache(attachment.url)
            if cached:
                media_paths.append(str(cached))

        # Determine message type
        msg_type = MessageType.TEXT
        if message.attachments:
            ct = message.attachments[0].content_type or ""
            if ct.startswith("image/"):
                msg_type = MessageType.PHOTO
            elif ct.startswith("audio/"):
                msg_type = MessageType.AUDIO
            else:
                msg_type = MessageType.DOCUMENT

        text = message.content or ""
        if not text and media_urls:
            text = f"[Attachment: {message.attachments[0].filename}]"

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=str(message.id),
            media_urls=media_urls,
            media_paths=media_paths,
            raw=message,
        )
        await self._dispatch_message(event)

    async def list_channels(self) -> list[dict[str, Any]]:
        """List channels from all guilds the bot is in."""
        if not self._client:
            return []

        channels: list[dict[str, Any]] = []
        for guild in self._client.guilds:
            for channel in guild.text_channels:
                channels.append({
                    "chat_id": str(channel.id),
                    "name": f"{guild.name}#{channel.name}",
                    "type": "channel",
                    "member_count": guild.member_count or 0,
                })
        return channels
