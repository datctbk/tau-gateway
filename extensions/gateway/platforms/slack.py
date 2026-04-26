"""Slack platform adapter.

Uses slack-bolt to connect tau to Slack workspaces.
Supports text messages, mentions, and thread replies.

Install: pip install slack-bolt
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


class SlackAdapter(BasePlatformAdapter):
    """Slack adapter using slack-bolt."""

    MAX_MESSAGE_LENGTH = 4000  # Slack limit is ~4000 for mrkdwn
    PLATFORM = Platform.SLACK

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self._app: Any = None
        self._bot_user_id: str = ""

    async def start(self) -> None:
        """Initialize Slack bolt app and start socket mode."""
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        except ImportError:
            raise ImportError(
                "slack-bolt is required: pip install slack-bolt"
            )

        if not self.config.token:
            raise ValueError("Slack token is required. Set TAU_GATEWAY_SLACK_TOKEN.")

        app_token = self.config.extra.get("app_token", "")
        if not app_token:
            import os
            app_token = os.environ.get("TAU_GATEWAY_SLACK_APP_TOKEN", "")

        self._app = AsyncApp(token=self.config.token)

        # Get bot user ID
        try:
            auth_response = await self._app.client.auth_test()
            self._bot_user_id = auth_response["user_id"]
        except Exception as exc:
            logger.warning("Failed to get bot user ID: %s", exc)

        # Register message handler
        @self._app.message("")
        async def handle_message(message, say):
            await self._on_message(message, say)

        # Start socket mode if app_token provided
        if app_token:
            handler = AsyncSocketModeHandler(self._app, app_token)
            asyncio.create_task(handler.start_async())
        else:
            logger.warning(
                "No Slack app_token — running in webhook mode. "
                "Set TAU_GATEWAY_SLACK_APP_TOKEN for socket mode."
            )

        self._running = True
        self._start_time = time.time()
        logger.info("Slack adapter started (bot_id=%s)", self._bot_user_id)

    async def stop(self) -> None:
        """Stop the Slack adapter."""
        self._running = False
        logger.info("Slack adapter stopped")

    async def send(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """Send a message to a Slack channel."""
        if not self._app:
            return SendResult(success=False, error="Adapter not started")

        try:
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "text": text[:self.MAX_MESSAGE_LENGTH],
            }
            if thread_id:
                kwargs["thread_ts"] = thread_id

            response = await self._app.client.chat_postMessage(**kwargs)
            msg_ts = response.get("ts", "")
            return SendResult(success=True, message_id=msg_ts)
        except Exception as exc:
            error_msg = str(exc)
            retryable = "ratelimited" in error_msg.lower()
            return SendResult(success=False, error=error_msg, retryable=retryable)

    # ── Inbound ──

    async def _on_message(self, message: dict, say: Any) -> None:
        """Handle a Slack message event."""
        # Ignore bot messages
        if message.get("bot_id") or message.get("subtype"):
            return

        text = message.get("text", "")
        user_id = message.get("user", "")
        channel_id = message.get("channel", "")
        thread_ts = message.get("thread_ts")
        ts = message.get("ts", "")

        # Check reply_to_mode
        if self.config.reply_to_mode == "mention":
            if f"<@{self._bot_user_id}>" not in text:
                return
            # Strip the mention from the text
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()

        # Get channel info
        channel_name = channel_id
        chat_type = "channel"
        try:
            info = await self._app.client.conversations_info(channel=channel_id)
            ch = info.get("channel", {})
            channel_name = ch.get("name", channel_id)
            if ch.get("is_im"):
                chat_type = "dm"
            elif ch.get("is_group"):
                chat_type = "group"
        except Exception:
            pass

        # Get user info
        user_name = user_id
        try:
            user_info = await self._app.client.users_info(user=user_id)
            user_name = user_info.get("user", {}).get("real_name", user_id)
        except Exception:
            pass

        source = SessionSource(
            platform="slack",
            chat_id=channel_id,
            chat_name=channel_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_ts,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=ts,
            raw=message,
        )
        await self._dispatch_message(event)

    async def list_channels(self) -> list[dict[str, Any]]:
        """List channels the bot is in."""
        if not self._app:
            return []

        channels: list[dict[str, Any]] = []
        try:
            response = await self._app.client.conversations_list(
                types="public_channel,private_channel"
            )
            for ch in response.get("channels", []):
                if ch.get("is_member"):
                    channels.append({
                        "chat_id": ch["id"],
                        "name": ch.get("name", ch["id"]),
                        "type": "channel",
                        "member_count": ch.get("num_members", 0),
                    })
        except Exception as exc:
            logger.warning("Failed to list Slack channels: %s", exc)

        return channels
