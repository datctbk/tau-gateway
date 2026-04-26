"""Tests for channel directory."""

import pytest
from extensions.gateway.channel_directory import ChannelDirectory, ChannelInfo
from extensions.gateway.types import SessionSource


@pytest.fixture
def directory():
    d = ChannelDirectory()
    d.add_channel(ChannelInfo("telegram", "123", "Dev Chat", "group"))
    d.add_channel(ChannelInfo("telegram", "456", "General", "group"))
    d.add_channel(ChannelInfo("discord", "789", "dev-team", "channel"))
    d.add_channel(ChannelInfo("discord", "101", "random", "channel"))
    d.add_channel(ChannelInfo("slack", "C123", "engineering", "channel"))
    return d


class TestChannelInfo:
    def test_key(self):
        ch = ChannelInfo("telegram", "123", "Test")
        assert ch.key == "telegram:123"

    def test_to_dict(self):
        ch = ChannelInfo("discord", "456", "Dev", "channel", 50)
        d = ch.to_dict()
        assert d["platform"] == "discord"
        assert d["chat_id"] == "456"
        assert d["name"] == "Dev"
        assert d["member_count"] == 50


class TestResolve:
    def test_exact_key(self, directory):
        ch = directory.resolve("telegram:123")
        assert ch is not None
        assert ch.name == "Dev Chat"

    def test_exact_name(self, directory):
        ch = directory.resolve("dev-team")
        assert ch is not None
        assert ch.platform == "discord"

    def test_platform_scoped_name(self, directory):
        ch = directory.resolve("discord:dev-team")
        assert ch is not None
        assert ch.chat_id == "789"

    def test_prefix_match(self, directory):
        ch = directory.resolve("General")
        # Should match "General" since it's a prefix (case insensitive)
        assert ch is not None
        assert ch.name == "General"

    def test_substring_match(self, directory):
        ch = directory.resolve("engineer")
        assert ch is not None
        assert ch.name == "engineering"

    def test_no_match(self, directory):
        assert directory.resolve("nonexistent") is None

    def test_ambiguous_match_returns_none(self):
        d = ChannelDirectory()
        d.add_channel(ChannelInfo("telegram", "1", "dev-chat"))
        d.add_channel(ChannelInfo("discord", "2", "dev-team"))
        # "dev" matches both as prefix → ambiguous
        result = d.resolve("dev")
        assert result is None


class TestListing:
    def test_list_all(self, directory):
        channels = directory.list_all()
        assert len(channels) == 5

    def test_list_by_platform(self, directory):
        channels = directory.list_all(platform="discord")
        assert len(channels) == 2
        assert all(ch.platform == "discord" for ch in channels)

    def test_sorted_by_platform_then_name(self, directory):
        channels = directory.list_all()
        platforms = [ch.platform for ch in channels]
        assert platforms == sorted(platforms)


class TestFormatting:
    def test_format_for_display(self, directory):
        text = directory.format_for_display()
        assert "Available channels" in text
        assert "telegram" in text
        assert "discord" in text

    def test_format_for_model(self, directory):
        text = directory.format_for_model()
        assert "telegram:123" in text
        assert "discord:789" in text

    def test_format_empty(self):
        d = ChannelDirectory()
        assert "No channels" in d.format_for_display()
        assert "No messaging channels" in d.format_for_model()


class TestAddFromSource:
    def test_add_from_source(self):
        d = ChannelDirectory()
        src = SessionSource(
            platform="telegram",
            chat_id="999",
            chat_name="New Chat",
            chat_type="private",
        )
        d.add_from_source(src)
        ch = d.resolve("telegram:999")
        assert ch is not None
        assert ch.name == "New Chat"
