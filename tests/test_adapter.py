"""Tests for BasePlatformAdapter — message splitting and media cache."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from extensions.gateway.adapter import BasePlatformAdapter
from extensions.gateway.config import Platform, PlatformConfig
from extensions.gateway.types import SendResult


class MockAdapter(BasePlatformAdapter):
    """Concrete adapter for testing the abstract base."""

    MAX_MESSAGE_LENGTH = 100
    PLATFORM = Platform.LOCAL

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, chat_id, text, thread_id=None, reply_to=None, parse_mode=None):
        return SendResult(success=True, message_id="mock-1")


@pytest.fixture
def adapter():
    config = PlatformConfig(platform=Platform.LOCAL, enabled=True)
    return MockAdapter(config)


class TestMessageSplitting:
    def test_short_message_no_split(self, adapter):
        chunks = adapter.split_message("Hello world")
        assert chunks == ["Hello world"]

    def test_split_at_paragraph(self, adapter):
        text = "A" * 60 + "\n\n" + "B" * 60
        chunks = adapter.split_message(text)
        assert len(chunks) == 2
        assert chunks[0].strip() == "A" * 60
        assert "B" in chunks[1]

    def test_split_at_newline(self, adapter):
        text = "A" * 50 + "\n" + "B" * 50
        chunks = adapter.split_message(text)
        assert len(chunks) == 2

    def test_split_at_word_boundary(self, adapter):
        words = ["word"] * 30  # each is 4 chars + space = 5
        text = " ".join(words)  # 149 chars
        chunks = adapter.split_message(text)
        assert len(chunks) >= 2
        # No chunk should cut mid-word
        for chunk in chunks:
            assert not chunk.startswith(" ")

    def test_hard_split_no_spaces(self, adapter):
        text = "X" * 250
        chunks = adapter.split_message(text)
        assert len(chunks) >= 3
        total = sum(len(c) for c in chunks)
        assert total == 250

    def test_custom_max_length(self, adapter):
        text = "A" * 200
        chunks = adapter.split_message(text, max_length=50)
        assert all(len(c) <= 50 for c in chunks)

    def test_empty_chunks_removed(self, adapter):
        text = "Hello\n\n\n\n\nWorld"
        chunks = adapter.split_message(text)
        assert all(c.strip() for c in chunks)


class TestMediaCache:
    def test_cache_media(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "extensions.gateway.adapter.MEDIA_CACHE_DIR", tmp_path
        )
        data = b"fake image data"
        path = BasePlatformAdapter.cache_media(
            "https://example.com/photo.jpg", data, ".jpg"
        )
        assert path.exists()
        assert path.suffix == ".jpg"
        assert path.read_bytes() == data

    def test_cache_deduplication(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "extensions.gateway.adapter.MEDIA_CACHE_DIR", tmp_path
        )
        data = b"same data"
        path1 = BasePlatformAdapter.cache_media("url1", data)
        path2 = BasePlatformAdapter.cache_media("url2", data)
        assert path1 == path2  # Same content hash → same file


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, adapter):
        assert adapter.is_running is False
        await adapter.start()
        assert adapter.is_running is True
        await adapter.stop()
        assert adapter.is_running is False

    def test_status(self, adapter):
        status = adapter.status()
        assert status["platform"] == "local"
        assert status["running"] is False

    @pytest.mark.asyncio
    async def test_send_chunked(self, adapter):
        text = "A" * 250
        results = await adapter.send_chunked("chat1", text)
        assert len(results) >= 3
        assert all(r.success for r in results)
