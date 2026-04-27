"""Telegram platform adapter.

Uses python-telegram-bot to connect tau to Telegram chats.
Supports text, photos, documents, voice messages, and commands.

Install: pip install python-telegram-bot
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..adapter import BasePlatformAdapter
from ..config import PlatformConfig, Platform
from ..types import MessageEvent, MessageType, SendResult, SessionSource

logger = logging.getLogger(__name__)


class TelegramAdapter(BasePlatformAdapter):
    """Telegram adapter using python-telegram-bot."""

    MAX_MESSAGE_LENGTH = 4096
    PLATFORM = Platform.TELEGRAM

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self._app: Any = None

    async def start(self) -> None:
        """Connect to Telegram and start polling for updates."""
        try:
            from telegram import Update
            from telegram.ext import (
                ApplicationBuilder,
                MessageHandler,
                filters,
            )
        except ImportError:
            raise ImportError(
                "python-telegram-bot is required: pip install python-telegram-bot"
            )

        if not self.config.token:
            raise ValueError("Telegram token is required. Set TAU_GATEWAY_TELEGRAM_TOKEN.")

        self._app = (
            ApplicationBuilder()
            .token(self.config.token)
            .build()
        )

        # Register handlers
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_message,
            )
        )
        # Capture all slash commands (e.g. /status, /status@BotName, /channels).
        self._app.add_handler(MessageHandler(filters.COMMAND, self._on_command))
        self._app.add_handler(
            MessageHandler(filters.PHOTO, self._on_photo)
        )
        self._app.add_handler(
            MessageHandler(filters.Document.ALL, self._on_document)
        )
        self._app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice)
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        self._running = True
        import time
        self._start_time = time.time()
        logger.info("Telegram adapter started")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as exc:
                logger.warning("Error stopping Telegram adapter: %s", exc)
        self._running = False
        logger.info("Telegram adapter stopped")

    async def send(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """Send a text message to a Telegram chat."""
        if not self._app:
            return SendResult(success=False, error="Adapter not started")

        try:
            kwargs: dict[str, Any] = {
                "chat_id": int(chat_id),
                "text": text[:self.MAX_MESSAGE_LENGTH],
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            if thread_id:
                kwargs["message_thread_id"] = int(thread_id)
            if reply_to:
                kwargs["reply_to_message_id"] = int(reply_to)

            msg = await self._app.bot.send_message(**kwargs)
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as exc:
            error_msg = str(exc)
            retryable = "retry" in error_msg.lower() or "flood" in error_msg.lower()
            return SendResult(success=False, error=error_msg, retryable=retryable)

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._app:
            try:
                await self._app.bot.send_chat_action(
                    chat_id=int(chat_id), action="typing"
                )
            except Exception:
                pass

    # ── Inbound handlers ──

    def _build_source(self, update: Any) -> SessionSource:
        """Extract SessionSource from a Telegram update."""
        chat = update.effective_chat
        user = update.effective_user

        # Detect forum topic thread_id
        thread_id = None
        if hasattr(update.effective_message, "message_thread_id"):
            tid = update.effective_message.message_thread_id
            if tid:
                thread_id = str(tid)

        return SessionSource(
            platform="telegram",
            chat_id=str(chat.id),
            chat_name=chat.title or chat.first_name or str(chat.id),
            chat_type=chat.type,
            user_id=str(user.id) if user else "",
            user_name=user.username or user.first_name or "" if user else "",
            thread_id=thread_id,
        )

    async def _on_text_message(self, update: Any, context: Any) -> None:
        """Handle inbound text messages."""
        if not update.message or not update.message.text:
            return

        source = self._build_source(update)

        # Check reply_to_mode
        if self.config.reply_to_mode == "mention":
            bot_username = (await self._app.bot.get_me()).username
            if f"@{bot_username}" not in update.message.text and update.effective_chat.type != "private":
                return

        event = MessageEvent(
            text=update.message.text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(update.message.message_id),
            raw=update,
        )
        await self._dispatch_message(event)

    async def _on_command(self, update: Any, context: Any) -> None:
        """Handle /commands."""
        if not update.message or not update.message.text:
            return

        source = self._build_source(update)
        event = MessageEvent(
            text=update.message.text,
            message_type=MessageType.COMMAND,
            source=source,
            message_id=str(update.message.message_id),
            raw=update,
        )
        await self._dispatch_message(event)

    async def _on_photo(self, update: Any, context: Any) -> None:
        """Handle photo messages."""
        if not update.message:
            return

        source = self._build_source(update)
        caption = update.message.caption or ""

        # Download the largest photo
        media_urls: list[str] = []
        media_paths: list[str] = []
        if update.message.photo:
            photo = update.message.photo[-1]  # Largest size
            try:
                file = await self._app.bot.get_file(photo.file_id)
                url = file.file_path
                if url:
                    media_urls.append(url)
                    cached = await self.download_and_cache(url)
                    if cached:
                        media_paths.append(str(cached))
            except Exception as exc:
                logger.warning("Failed to download photo: %s", exc)

        event = MessageEvent(
            text=caption or "[Photo]",
            message_type=MessageType.PHOTO,
            source=source,
            message_id=str(update.message.message_id),
            media_urls=media_urls,
            media_paths=media_paths,
            raw=update,
        )
        await self._dispatch_message(event)

    async def _on_document(self, update: Any, context: Any) -> None:
        """Handle document messages."""
        if not update.message or not update.message.document:
            return

        source = self._build_source(update)
        doc = update.message.document
        caption = update.message.caption or f"[Document: {doc.file_name}]"

        media_paths: list[str] = []
        try:
            file = await self._app.bot.get_file(doc.file_id)
            if file.file_path:
                cached = await self.download_and_cache(file.file_path)
                if cached:
                    media_paths.append(str(cached))
        except Exception as exc:
            logger.warning("Failed to download document: %s", exc)

        event = MessageEvent(
            text=caption,
            message_type=MessageType.DOCUMENT,
            source=source,
            message_id=str(update.message.message_id),
            media_paths=media_paths,
            raw=update,
        )
        await self._dispatch_message(event)

    async def _on_voice(self, update: Any, context: Any) -> None:
        """Handle voice/audio messages."""
        if not update.message:
            return

        source = self._build_source(update)
        voice = update.message.voice or update.message.audio

        media_paths: list[str] = []
        if voice:
            try:
                file = await self._app.bot.get_file(voice.file_id)
                if file.file_path:
                    cached = await self.download_and_cache(file.file_path)
                    if cached:
                        media_paths.append(str(cached))
            except Exception as exc:
                logger.warning("Failed to download voice: %s", exc)

        event = MessageEvent(
            text="[Voice message]",
            message_type=MessageType.VOICE,
            source=source,
            message_id=str(update.message.message_id),
            media_paths=media_paths,
            raw=update,
        )
        await self._dispatch_message(event)

    async def list_channels(self) -> list[dict[str, Any]]:
        """Telegram doesn't have a list-channels API for bots."""
        return []
