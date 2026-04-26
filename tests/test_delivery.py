"""Tests for delivery router."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from extensions.gateway.delivery import DeliveryRouter
from extensions.gateway.types import DeliveryTarget, SendResult, SessionSource


@pytest.fixture
def router():
    return DeliveryRouter()


@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.send_chunked = AsyncMock(
        return_value=[SendResult(success=True, message_id="m1")]
    )
    return adapter


class TestDeliveryRouter:
    def test_register_adapter(self, router, mock_adapter):
        router.register_adapter("telegram", mock_adapter)
        assert "telegram" in router.registered_platforms

    def test_get_adapter(self, router, mock_adapter):
        router.register_adapter("telegram", mock_adapter)
        assert router.get_adapter("telegram") is mock_adapter
        assert router.get_adapter("discord") is None


class TestTargetResolution:
    def test_resolve_origin(self, router):
        src = SessionSource(platform="telegram", chat_id="123")
        targets = router.resolve_targets(src)
        assert len(targets) == 1
        assert targets[0].is_origin is True
        assert targets[0].platform == "telegram"

    def test_resolve_with_home_channels(self, router):
        src = SessionSource(platform="telegram", chat_id="123")
        targets = router.resolve_targets(
            src,
            home_channels=[
                {"platform": "discord", "chat_id": "456"},
            ],
            echo_to_home=True,
        )
        assert len(targets) == 2
        assert targets[0].is_origin is True
        assert targets[1].platform == "discord"

    def test_no_duplicate_origin(self, router):
        src = SessionSource(platform="telegram", chat_id="123")
        targets = router.resolve_targets(
            src,
            home_channels=[
                {"platform": "telegram", "chat_id": "123"},
            ],
            echo_to_home=True,
        )
        assert len(targets) == 1  # Deduped


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_success(self, router, mock_adapter):
        router.register_adapter("telegram", mock_adapter)
        target = DeliveryTarget(platform="telegram", chat_id="123")
        results = await router.dispatch("Hello!", [target])
        assert len(results) == 1
        assert results[0].success is True
        mock_adapter.send_chunked.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_no_adapter(self, router):
        target = DeliveryTarget(platform="unknown", chat_id="123")
        results = await router.dispatch("Hello!", [target])
        assert len(results) == 1
        assert results[0].success is False
        assert "No adapter" in results[0].error

    @pytest.mark.asyncio
    async def test_send_to(self, router, mock_adapter):
        router.register_adapter("telegram", mock_adapter)
        result = await router.send_to("telegram", "123", "Hi!")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_to_missing_adapter(self, router):
        result = await router.send_to("telegram", "123", "Hi!")
        assert result.success is False
