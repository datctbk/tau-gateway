"""Gateway runner — the main orchestrator.

Loads configuration, initializes platform adapters, and runs the event
loop that processes inbound messages through the agent and delivers
responses back to the originating platform.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import GatewayConfig, Platform, load_gateway_config
from .types import MessageEvent, SendResult, SessionSource
from .session import SessionStore, SessionEntry, build_session_context_prompt
from .delivery import DeliveryRouter
from .hooks import HookRegistry
from .channel_directory import ChannelDirectory

logger = logging.getLogger(__name__)


class GatewayRunner:
    """Main gateway orchestrator.

    1. Loads config from ~/.tau/gateway.yaml
    2. Initializes enabled platform adapters
    3. Sets up the delivery router and session store
    4. Runs the async event loop processing messages

    Usage::

        runner = GatewayRunner()
        await runner.run()
    """

    def __init__(
        self,
        config: GatewayConfig | None = None,
        config_path: Path | None = None,
    ):
        self.config = config or load_gateway_config(config_path)
        self._adapters: Dict[str, Any] = {}
        self._delivery = DeliveryRouter()
        self._sessions = SessionStore(reset_policy=self.config.reset_policy)
        self._hooks = HookRegistry()
        self._channels = ChannelDirectory()
        self._running = False
        self._start_time: float | None = None

        # Optional: SQLite state store integration
        self._state_db: Any = None

    def _init_state_db(self) -> None:
        """Try to initialize the SQLite state store."""
        try:
            from tau.core.state import SessionDB
            self._state_db = SessionDB()
            logger.info("SQLite state store initialized at %s", self._state_db.db_path)
        except ImportError:
            logger.debug("tau.core.state not available — running without persistent state")
        except Exception as exc:
            logger.warning("Failed to init state DB: %s", exc)

    # ── Adapter initialization ──

    def _init_adapters(self) -> None:
        """Initialize platform adapters based on config."""
        for pc in self.config.enabled_platforms():
            try:
                adapter = self._create_adapter(pc.platform, pc)
                if adapter is not None:
                    adapter.set_message_handler(self._handle_message)
                    self._adapters[pc.platform.value] = adapter
                    self._delivery.register_adapter(pc.platform.value, adapter)
                    logger.info("Initialized %s adapter", pc.platform.value)
            except Exception as exc:
                logger.error(
                    "Failed to init %s adapter: %s", pc.platform.value, exc
                )

    def _create_adapter(self, platform: Platform, config: Any) -> Optional[Any]:
        """Create a platform adapter instance."""
        if platform == Platform.TELEGRAM:
            from .platforms.telegram import TelegramAdapter
            return TelegramAdapter(config)
        elif platform == Platform.DISCORD:
            from .platforms.discord import DiscordAdapter
            return DiscordAdapter(config)
        elif platform == Platform.SLACK:
            from .platforms.slack import SlackAdapter
            return SlackAdapter(config)
        elif platform == Platform.API_SERVER:
            from .platforms.api_server import ApiServerAdapter
            return ApiServerAdapter(config)
        else:
            logger.warning("Unknown platform: %s", platform.value)
            return None

    # ── Message handling ──

    async def _handle_message(self, event: MessageEvent) -> None:
        """Process an inbound message: session lookup → agent → deliver response."""
        source = event.source

        # Record channel in directory
        self._channels.add_from_source(source)

        # Emit hook
        self._hooks.emit("message:inbound", event.to_dict())

        # Handle commands
        if event.text.startswith("/"):
            handled = await self._handle_command(event)
            if handled:
                return

        # Get or create session
        entry, is_new = self._sessions.get_or_create(source)

        if is_new:
            self._hooks.emit("session:start", {
                "session_id": entry.session_id,
                "source": source.to_dict(),
            })
            if self._state_db:
                self._state_db.create_session(
                    session_id=entry.session_id,
                    source=source.platform,
                    model=self.config.model,
                    user_id=source.user_id,
                )

        # Record message in state DB
        if self._state_db:
            self._state_db.append_message(
                session_id=entry.session_id,
                role="user",
                content=event.text,
            )

        # Send typing indicator
        adapter = self._adapters.get(source.platform)
        if adapter:
            try:
                await adapter.send_typing(source.chat_id)
            except Exception:
                pass

        # Run the agent
        response_text = await self._run_agent(entry, event)

        # Record response in state DB
        if self._state_db and response_text:
            self._state_db.append_message(
                session_id=entry.session_id,
                role="assistant",
                content=response_text,
            )

        # Deliver response
        if response_text:
            targets = self._delivery.resolve_targets(source)
            results = await self._delivery.dispatch(response_text, targets)
            self._hooks.emit("message:outbound", {
                "session_id": entry.session_id,
                "text_length": len(response_text),
                "targets": len(targets),
                "results": [r.to_dict() for r in results],
            })

    async def _handle_command(self, event: MessageEvent) -> bool:
        """Handle gateway commands like /new, /reset, /status."""
        text_lower = event.text.lower().strip()

        if text_lower in ("/new", "/reset"):
            entry = self._sessions.reset_by_source(event.source)
            if entry:
                self._hooks.emit("session:reset", {
                    "session_id": entry.session_id,
                    "source": event.source.to_dict(),
                })
                if self._state_db:
                    self._state_db.end_session(entry.session_id, "manual_reset")

            await self._delivery.send_to(
                event.source.platform,
                event.source.chat_id,
                "🔄 Session reset. Starting fresh.",
                thread_id=event.source.thread_id,
            )
            return True

        if text_lower == "/status":
            status = self._format_status()
            await self._delivery.send_to(
                event.source.platform,
                event.source.chat_id,
                status,
                thread_id=event.source.thread_id,
            )
            return True

        if text_lower == "/channels":
            directory = self._channels.format_for_display()
            await self._delivery.send_to(
                event.source.platform,
                event.source.chat_id,
                directory,
                thread_id=event.source.thread_id,
            )
            return True

        # Unknown command — emit hook and fall through
        self._hooks.emit("command:unknown", {
            "command": event.text,
            "source": event.source.to_dict(),
        })
        return False

    async def _run_agent(self, entry: SessionEntry, event: MessageEvent) -> str:
        """Run the tau agent for a message. Returns the response text.

        This is the integration point with the tau agent loop. In standalone
        mode, it creates a sub-session via the SDK. When running as an
        extension, it uses the extension context.
        """
        try:
            from tau.sdk import create_session

            context_prompt = build_session_context_prompt(
                source=event.source,
                connected_platforms=list(self._adapters.keys()),
            )

            system_prompt = self.config.system_prompt or ""
            if context_prompt:
                system_prompt = f"{system_prompt}\n\n{context_prompt}" if system_prompt else context_prompt

            self._hooks.emit("agent:start", {
                "session_id": entry.session_id,
                "source": event.source.to_dict(),
            })

            with create_session(
                provider=self.config.provider,
                model=self.config.model,
                system_prompt=system_prompt,
                workspace=".",
                session_name=f"gateway-{event.source.platform}-{entry.session_key[:8]}",
                in_memory=True,
                load_extensions=True,
                load_skills=True,
            ) as sub:
                from tau.core.types import TextDelta
                events = sub.prompt_sync(event.text)
                response = "".join(
                    e.text for e in events
                    if isinstance(e, TextDelta) and not getattr(e, "is_thinking", False)
                )

            self._hooks.emit("agent:end", {
                "session_id": entry.session_id,
                "response_length": len(response),
            })

            return response.strip()

        except ImportError:
            logger.warning("tau.sdk not available — returning echo response")
            return f"[echo] {event.text}"
        except Exception as exc:
            logger.error("Agent run failed: %s", exc, exc_info=True)
            self._hooks.emit("agent:error", {
                "session_id": entry.session_id,
                "error": str(exc),
            })
            return f"⚠️ Error processing your message: {exc}"

    # ── Status ──

    def _format_status(self) -> str:
        """Format gateway status for display."""
        lines = ["🤖 **Gateway Status**", ""]

        # Uptime
        if self._start_time:
            uptime = time.time() - self._start_time
            hours = int(uptime // 3600)
            mins = int((uptime % 3600) // 60)
            lines.append(f"⏱️ Uptime: {hours}h {mins}m")

        # Adapters
        lines.append(f"\n📡 **Platforms** ({len(self._adapters)} connected):")
        for name, adapter in self._adapters.items():
            status = adapter.status()
            running = "✅" if status.get("running") else "❌"
            lines.append(f"  {running} {name}")

        # Sessions
        active = self._sessions.session_count
        lines.append(f"\n💬 **Sessions:** {active} active")

        # Channels
        channels = self._channels.list_all()
        lines.append(f"📋 **Known channels:** {len(channels)}")

        return "\n".join(lines)

    # ── Lifecycle ──

    async def run(self) -> None:
        """Start the gateway and run until interrupted."""
        logger.info("Starting tau-gateway...")

        self._init_state_db()
        self._init_adapters()
        self._hooks.discover()

        if not self._adapters:
            logger.error(
                "No platform adapters configured. "
                "Set up platforms in ~/.tau/gateway.yaml or via environment variables:\n"
                "  TAU_GATEWAY_TELEGRAM_TOKEN=...\n"
                "  TAU_GATEWAY_DISCORD_TOKEN=...\n"
                "  TAU_GATEWAY_SLACK_TOKEN=...\n"
                "  TAU_GATEWAY_API_SERVER_API_KEY=..."
            )
            return

        self._running = True
        self._start_time = time.time()

        self._hooks.emit("gateway:startup", {
            "platforms": list(self._adapters.keys()),
            "config": self.config.to_dict(),
        })

        # Start all adapters
        start_tasks = []
        for name, adapter in self._adapters.items():
            logger.info("Starting %s adapter...", name)
            start_tasks.append(adapter.start())

        if start_tasks:
            await asyncio.gather(*start_tasks, return_exceptions=True)

        # Refresh channel directory
        await self._channels.refresh_from_adapters(self._adapters)

        platforms = ", ".join(self._adapters.keys())
        logger.info("Gateway running with platforms: %s", platforms)
        print(f"[gateway] Running with platforms: {platforms}")
        print("[gateway] Press Ctrl+C to stop")

        # Keep running until shutdown
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass  # Windows

        await stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        """Gracefully shut down all adapters."""
        logger.info("Shutting down gateway...")
        self._running = False

        self._hooks.emit("gateway:shutdown", {
            "uptime_seconds": time.time() - (self._start_time or 0),
        })

        stop_tasks = []
        for name, adapter in self._adapters.items():
            logger.info("Stopping %s adapter...", name)
            stop_tasks.append(adapter.stop())

        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        if self._state_db:
            self._state_db.close()

        logger.info("Gateway shutdown complete")
