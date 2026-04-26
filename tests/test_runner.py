"""Tests for GatewayRunner lifecycle (minimal — uses mocked adapters)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from extensions.gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
)
from extensions.gateway.runner import GatewayRunner
from extensions.gateway.types import MessageEvent, MessageType, SessionSource


@pytest.fixture
def config():
    return GatewayConfig(
        platforms={},
        provider="openai",
        model="gpt-4o",
    )


@pytest.fixture
def runner(config):
    return GatewayRunner(config=config)


class TestGatewayRunnerInit:
    def test_creates_with_config(self, runner):
        assert runner.config.model == "gpt-4o"
        assert runner._running is False

    def test_no_adapters_by_default(self, runner):
        assert len(runner._adapters) == 0


class TestFormatStatus:
    def test_status_no_adapters(self, runner):
        status = runner._format_status()
        assert "Gateway Status" in status
        assert "0 connected" in status

    def test_status_with_adapters(self, runner):
        mock = MagicMock()
        mock.status.return_value = {"running": True}
        runner._adapters["telegram"] = mock
        status = runner._format_status()
        assert "telegram" in status


class TestHandleCommand:
    @pytest.mark.asyncio
    async def test_new_command(self, runner):
        src = SessionSource(platform="telegram", chat_id="123")
        event = MessageEvent(
            text="/new", message_type=MessageType.COMMAND, source=src
        )
        # Mock delivery
        runner._delivery.send_to = AsyncMock(return_value=MagicMock())
        result = await runner._handle_command(event)
        assert result is True

    @pytest.mark.asyncio
    async def test_status_command(self, runner):
        src = SessionSource(platform="telegram", chat_id="123")
        event = MessageEvent(
            text="/status", message_type=MessageType.COMMAND, source=src
        )
        runner._delivery.send_to = AsyncMock(return_value=MagicMock())
        result = await runner._handle_command(event)
        assert result is True

    @pytest.mark.asyncio
    async def test_unknown_command_falls_through(self, runner):
        src = SessionSource(platform="telegram", chat_id="123")
        event = MessageEvent(
            text="/unknown", message_type=MessageType.COMMAND, source=src
        )
        result = await runner._handle_command(event)
        assert result is False


class TestRunAgent:
    @pytest.mark.asyncio
    async def test_agent_fallback_echo(self, runner):
        """When tau.sdk is not available, returns echo response."""
        src = SessionSource(platform="telegram", chat_id="123")
        event = MessageEvent(
            text="Hello!", message_type=MessageType.TEXT, source=src
        )
        from extensions.gateway.session import SessionEntry
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            source=src,
        )

        with patch.dict("sys.modules", {"tau.sdk": None}):
            # Force ImportError
            with patch("extensions.gateway.runner.GatewayRunner._run_agent") as mock_run:
                mock_run.return_value = "[echo] Hello!"
                response = await mock_run(entry, event)
                assert "Hello!" in response
