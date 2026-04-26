"""Delivery router — resolves targets and dispatches responses.

Given an inbound message source and configured home channels, the
delivery router determines where to send each response and dispatches
through the appropriate platform adapters.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .types import DeliveryTarget, SendResult, SessionSource

logger = logging.getLogger(__name__)


class DeliveryRouter:
    """Route outbound messages to the correct platform adapter.

    The router maintains a registry of platform adapters and resolves
    delivery targets from session sources and home channel configs.
    """

    def __init__(self):
        self._adapters: Dict[str, Any] = {}  # platform_name → BasePlatformAdapter

    def register_adapter(self, platform: str, adapter: Any) -> None:
        """Register a platform adapter for outbound delivery."""
        self._adapters[platform] = adapter
        logger.debug("Registered adapter for platform: %s", platform)

    def get_adapter(self, platform: str) -> Optional[Any]:
        """Get the adapter for a platform."""
        return self._adapters.get(platform)

    @property
    def registered_platforms(self) -> list[str]:
        """List of registered platform names."""
        return list(self._adapters.keys())

    # ── Target resolution ──

    def resolve_targets(
        self,
        source: SessionSource,
        home_channels: list[Dict[str, Any]] | None = None,
        echo_to_home: bool = False,
    ) -> list[DeliveryTarget]:
        """Resolve delivery targets for a response.

        Args:
            source: Where the inbound message came from (always a target).
            home_channels: Optional list of home channel dicts to also deliver to.
            echo_to_home: If True, also delivers to configured home channels.

        Returns:
            List of DeliveryTarget objects.
        """
        targets = [DeliveryTarget.from_source(source)]

        if echo_to_home and home_channels:
            for hc in home_channels:
                target = DeliveryTarget(
                    platform=hc.get("platform", ""),
                    chat_id=str(hc.get("chat_id", "")),
                    thread_id=hc.get("thread_id"),
                    is_origin=False,
                )
                # Don't duplicate the origin
                if target.key != targets[0].key:
                    targets.append(target)

        return targets

    # ── Dispatch ──

    async def dispatch(
        self,
        text: str,
        targets: list[DeliveryTarget],
        parse_mode: str | None = None,
    ) -> list[SendResult]:
        """Send text to all delivery targets.

        Returns a list of SendResult objects (one per target).
        """
        results: list[SendResult] = []

        for target in targets:
            adapter = self._adapters.get(target.platform)
            if adapter is None:
                results.append(SendResult(
                    success=False,
                    error=f"No adapter registered for platform: {target.platform}",
                ))
                continue

            try:
                chunk_results = await adapter.send_chunked(
                    chat_id=target.chat_id,
                    text=text,
                    thread_id=target.thread_id,
                    parse_mode=parse_mode,
                )
                results.extend(chunk_results)
            except Exception as exc:
                logger.error(
                    "Failed to deliver to %s: %s", target.key, exc, exc_info=True
                )
                results.append(SendResult(
                    success=False,
                    error=str(exc),
                    retryable=True,
                ))

        return results

    async def send_to(
        self,
        platform: str,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """Send a message to a specific platform:chat_id.

        Convenience method for direct sends (e.g. from the send_message tool).
        """
        adapter = self._adapters.get(platform)
        if adapter is None:
            return SendResult(
                success=False,
                error=f"No adapter registered for platform: {platform}",
            )

        try:
            results = await adapter.send_chunked(
                chat_id=chat_id,
                text=text,
                thread_id=thread_id,
                parse_mode=parse_mode,
            )
            # Return the last result (or first failure)
            for r in results:
                if not r.success:
                    return r
            return results[-1] if results else SendResult(success=False, error="No chunks sent")
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
