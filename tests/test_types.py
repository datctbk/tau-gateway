"""Tests for gateway types module."""

import time
import pytest

from extensions.gateway.types import (
    DeliveryTarget,
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
)


class TestSessionSource:
    def test_session_key(self):
        src = SessionSource(platform="telegram", chat_id="123")
        assert src.session_key() == "telegram:123"

    def test_session_key_with_thread(self):
        src = SessionSource(
            platform="discord", chat_id="456", thread_id="789"
        )
        assert src.session_key() == "discord:456:789"

    def test_is_frozen(self):
        src = SessionSource(platform="telegram", chat_id="123")
        with pytest.raises(AttributeError):
            src.platform = "discord"

    def test_display(self):
        src = SessionSource(
            platform="telegram", chat_id="123", chat_name="Dev Chat"
        )
        assert src.display() == "telegram:Dev Chat"

    def test_display_with_thread(self):
        src = SessionSource(
            platform="discord",
            chat_id="123",
            chat_name="general",
            thread_id="456",
        )
        assert src.display() == "discord:general#456"

    def test_roundtrip(self):
        src = SessionSource(
            platform="slack",
            chat_id="C123",
            chat_name="dev",
            chat_type="channel",
            user_id="U456",
            user_name="alice",
        )
        d = src.to_dict()
        restored = SessionSource.from_dict(d)
        assert restored == src


class TestMessageEvent:
    def test_create(self):
        src = SessionSource(platform="telegram", chat_id="123")
        event = MessageEvent(
            text="Hello!",
            message_type=MessageType.TEXT,
            source=src,
        )
        assert event.text == "Hello!"
        assert event.message_type == MessageType.TEXT
        assert event.timestamp > 0

    def test_to_dict(self):
        src = SessionSource(platform="telegram", chat_id="123")
        event = MessageEvent(
            text="Test",
            message_type=MessageType.PHOTO,
            source=src,
            media_urls=["https://example.com/photo.jpg"],
        )
        d = event.to_dict()
        assert d["text"] == "Test"
        assert d["message_type"] == "photo"
        assert d["source"]["platform"] == "telegram"
        assert len(d["media_urls"]) == 1


class TestSendResult:
    def test_success(self):
        r = SendResult(success=True, message_id="msg123")
        assert r.success is True
        assert r.message_id == "msg123"

    def test_failure(self):
        r = SendResult(success=False, error="Rate limited", retryable=True)
        assert r.success is False
        assert r.retryable is True

    def test_to_dict(self):
        r = SendResult(success=True, message_id="m1")
        d = r.to_dict()
        assert d["success"] is True
        assert d["message_id"] == "m1"


class TestDeliveryTarget:
    def test_from_source(self):
        src = SessionSource(platform="telegram", chat_id="123", thread_id="99")
        dt = DeliveryTarget.from_source(src)
        assert dt.platform == "telegram"
        assert dt.chat_id == "123"
        assert dt.thread_id == "99"
        assert dt.is_origin is True

    def test_parse(self):
        dt = DeliveryTarget.parse("telegram:12345")
        assert dt.platform == "telegram"
        assert dt.chat_id == "12345"
        assert dt.thread_id is None

    def test_parse_with_thread(self):
        dt = DeliveryTarget.parse("discord:guild#thread")
        assert dt.platform == "discord"
        assert dt.chat_id == "guild"
        assert dt.thread_id == "thread"

    def test_parse_invalid(self):
        with pytest.raises(ValueError, match="Invalid target"):
            DeliveryTarget.parse("invalid")

    def test_key(self):
        dt = DeliveryTarget(platform="slack", chat_id="C123")
        assert dt.key == "slack:C123"

    def test_key_with_thread(self):
        dt = DeliveryTarget(platform="slack", chat_id="C123", thread_id="T456")
        assert dt.key == "slack:C123:T456"


class TestMessageType:
    def test_values(self):
        assert MessageType.TEXT == "text"
        assert MessageType.PHOTO == "photo"
        assert MessageType.VOICE == "voice"
