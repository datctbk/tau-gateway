"""tau-gateway: Multi-platform messaging gateway extension for tau.

Provides tools for cross-platform messaging and session search, plus
slash commands for gateway management. When loaded as a tau extension,
registers tools and commands; when run standalone, starts the full
gateway service.

Tools registered:
  - send_message    : Send text to any connected platform
  - session_search  : FTS5-powered cross-session search

Slash commands:
  /gateway   : Show gateway status
  /gateway-setup : Setup guide for gateway platforms
  /gateway-start : Start standalone gateway daemon
  /gateway-stop  : Stop standalone gateway daemon
  /send      : Quick-send to a platform target
  /channels  : List available messaging targets
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

from tau.core.extension import Extension, ExtensionContext
from tau.core.types import (
    ExtensionManifest,
    SlashCommand,
    ToolDefinition,
    ToolParameter,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class GatewayExtension(Extension):
    manifest = ExtensionManifest(
        name="gateway",
        version="0.1.0",
        description="Multi-platform messaging gateway — send messages, search sessions.",
        author="datctbk",
        system_prompt_fragment=(
            "You have access to a messaging gateway that can send messages "
            "to connected platforms (Telegram, Discord, Slack, API). "
            "Use send_message to reach specific chat targets. "
            "Use session_search to find information across past conversations."
        ),
    )

    def __init__(self) -> None:
        self._ext_context: ExtensionContext | None = None
        self._state_db: Any = None
        self._runner: Any = None
        self._channel_directory: Any = None

    def on_load(self, context: ExtensionContext) -> None:
        self._ext_context = context

        # Try to initialize state DB for session search
        try:
            from tau.core.state import SessionDB
            self._state_db = SessionDB()
            logger.debug("Gateway extension: state DB initialized")
        except ImportError:
            logger.debug("Gateway extension: tau.core.state not available")
        except Exception as exc:
            logger.warning("Gateway extension: failed to init state DB: %s", exc)

        # Initialize channel directory
        try:
            from .channel_directory import ChannelDirectory
            self._channel_directory = ChannelDirectory()
        except Exception:
            pass

    def on_unload(self) -> None:
        """Stop managed gateway when extension unloads (e.g. tau exit/reload)."""
        pid = self._read_managed_pid()
        if not pid or not self._is_pid_running(pid):
            self._clear_managed_pid()
            return
        # Only stop if it still looks like a gateway process.
        if not self._looks_like_gateway_pid(pid):
            self._clear_managed_pid()
            return
        self._terminate_pid(pid)
        self._clear_managed_pid()

    # ------------------------------------------------------------------
    # Standalone daemon helpers
    # ------------------------------------------------------------------

    def _gateway_runtime_dir(self) -> Path:
        p = Path.home() / ".tau" / "gateway"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _managed_pid_path(self) -> Path:
        return self._gateway_runtime_dir() / "gateway.pid"

    def _managed_log_path(self) -> Path:
        return self._gateway_runtime_dir() / "gateway.log"

    def _read_managed_pid(self) -> int | None:
        fp = self._managed_pid_path()
        if not fp.exists():
            return None
        try:
            raw = fp.read_text(encoding="utf-8").strip()
            # Backward/forward compatibility:
            # - "12345"
            # - "pid=12345"
            if raw.startswith("pid="):
                raw = raw.split("=", 1)[1].strip()
            return int(raw)
        except Exception:
            return None

    def _write_managed_pid(self, pid: int) -> None:
        self._managed_pid_path().write_text(str(pid), encoding="utf-8")

    def _clear_managed_pid(self) -> None:
        try:
            self._managed_pid_path().unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _pid_cmdline(pid: int) -> str:
        try:
            out = subprocess.check_output(
                ["ps", "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return out.strip()
        except Exception:
            return ""

    def _looks_like_gateway_pid(self, pid: int) -> bool:
        cmd = self._pid_cmdline(pid).lower()
        if not cmd:
            return False
        return "tau-gateway" in cmd or "extensions.gateway.runner" in cmd or "__main__.py" in cmd

    def _find_gateway_entrypoint(self) -> Path | None:
        # 0) Explicit override for deterministic startup source.
        override = os.environ.get("TAU_GATEWAY_ENTRYPOINT", "").strip()
        if override:
            p = Path(override).expanduser().resolve()
            if p.is_file():
                return p

        # 1) Canonical default: entrypoint next to the loaded extension package.
        #    This keeps local-dev and installed-package behavior consistent.
        pkg_root = Path(__file__).resolve().parents[2]
        canonical = pkg_root / "__main__.py"
        if canonical.is_file():
            return canonical

        # 2) Optional fallback for legacy workflows. Disabled by default.
        if os.environ.get("TAU_GATEWAY_ALLOW_CWD_ENTRYPOINT", "").strip() == "1":
            local = Path.cwd() / "tau-gateway" / "__main__.py"
            if local.is_file():
                return local

        return None

    @staticmethod
    def _list_gateway_pids() -> list[int]:
        """Return running gateway daemon pids discovered from process command lines."""
        try:
            out = subprocess.check_output(
                ["ps", "-axo", "pid=,command="],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            return []

        pids: list[int] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_str, cmd = parts
            try:
                pid = int(pid_str)
            except Exception:
                continue
            low = cmd.lower()
            if "tau-gateway/__main__.py" in low or "tau_gateway/__main__.py" in low:
                pids.append(pid)
        return pids

    @staticmethod
    def _terminate_pid(pid: int) -> bool:
        """Terminate a pid (and its process group) with graceful then forceful fallback."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return False

        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)

        # Escalate to process group
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass

        for _ in range(30):
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)

        if hasattr(signal, "SIGKILL"):
            try:
                pgid = os.getpgid(pid)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

        try:
            os.kill(pid, 0)
            return False
        except OSError:
            return True

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="send_message",
                description=(
                    "Send a message to a connected messaging platform.\n\n"
                    "Target format: 'platform:chat_id' (e.g. 'telegram:12345', "
                    "'discord:987654321', 'slack:C12345').\n\n"
                    "Use action='list' to see available targets.\n"
                    "Use action='resolve' with target as a human-friendly name "
                    "to resolve it to a chat_id."
                ),
                parameters={
                    "action": ToolParameter(
                        type="string",
                        description=(
                            "Action to perform: 'send' (default), 'list' (show targets), "
                            "'resolve' (name → chat_id)."
                        ),
                        required=False,
                    ),
                    "target": ToolParameter(
                        type="string",
                        description=(
                            "The target to send to (e.g. 'telegram:12345'). "
                            "For action='resolve', this is the human-friendly name."
                        ),
                        required=False,
                    ),
                    "message": ToolParameter(
                        type="string",
                        description="The message text to send.",
                        required=False,
                    ),
                },
                handler=self._handle_send_message,
            ),
            ToolDefinition(
                name="session_search",
                description=(
                    "Search across all past sessions using full-text search.\n\n"
                    "Supports keywords, phrases (\"exact match\"), and prefix matching (word*).\n"
                    "Returns matching message snippets with session context.\n\n"
                    "Use this to find previous conversations, decisions, code discussions, "
                    "or any information from past sessions."
                ),
                parameters={
                    "query": ToolParameter(
                        type="string",
                        description="Search query. Supports keywords, \"phrases\", and prefix*.",
                    ),
                    "source": ToolParameter(
                        type="string",
                        description=(
                            "Optional: filter by source platform "
                            "(e.g. 'cli', 'telegram', 'discord')."
                        ),
                        required=False,
                    ),
                    "role": ToolParameter(
                        type="string",
                        description=(
                            "Optional: filter by message role "
                            "(e.g. 'user', 'assistant', 'tool')."
                        ),
                        required=False,
                    ),
                    "limit": ToolParameter(
                        type="integer",
                        description="Max results to return (default: 10, max: 50).",
                        required=False,
                    ),
                },
                handler=self._handle_session_search,
            ),
        ]

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def slash_commands(self) -> list[SlashCommand]:
        return [
            SlashCommand(
                name="gateway",
                description="Show gateway status and connected platforms.",
                usage="/gateway",
            ),
            SlashCommand(
                name="send",
                description="Quick-send a message to a platform target.",
                usage="/send <target> <message>",
            ),
            SlashCommand(
                name="gateway-setup",
                description="Show setup steps for a gateway platform (e.g. telegram).",
                usage="/gateway-setup telegram",
            ),
            SlashCommand(
                name="gateway-start",
                description="Start standalone gateway daemon in the background.",
                usage="/gateway-start",
            ),
            SlashCommand(
                name="gateway-stop",
                description="Stop standalone gateway daemon started by /gateway-start.",
                usage="/gateway-stop",
            ),
            SlashCommand(
                name="channels",
                description="List available messaging targets.",
                usage="/channels [platform]",
            ),
        ]

    def handle_slash(self, command: str, args: str, context: ExtensionContext) -> bool:
        if command == "gateway":
            self._handle_gateway_status(context)
            return True
        elif command == "send":
            self._handle_send_slash(args, context)
            return True
        elif command == "gateway-setup":
            self._handle_gateway_setup_slash(args, context)
            return True
        elif command == "gateway-start":
            self._handle_gateway_start_slash(context)
            return True
        elif command == "gateway-stop":
            self._handle_gateway_stop_slash(context)
            return True
        elif command == "channels":
            self._handle_channels_slash(args, context)
            return True
        return False

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _handle_send_message(
        self,
        action: str = "send",
        target: str = "",
        message: str = "",
    ) -> str:
        action = (action or "send").lower()

        if action == "list":
            if self._channel_directory:
                return self._channel_directory.format_for_model()
            return "No channel directory available. Start the gateway first."

        if action == "resolve":
            if not target:
                return "Error: 'target' is required for resolve action."
            if self._channel_directory:
                ch = self._channel_directory.resolve(target)
                if ch:
                    return f"Resolved: {target} → {ch.key} ({ch.name})"
                return f"Could not resolve '{target}' to any known channel."
            return "No channel directory available."

        # action == "send"
        if not target:
            return "Error: 'target' is required. Format: 'platform:chat_id'"
        if not message:
            return "Error: 'message' is required."

        # Parse target
        try:
            from .types import DeliveryTarget
            dt = DeliveryTarget.parse(target)
        except ValueError as e:
            return f"Error: {e}"

        # Try to send via runner if available
        if self._runner:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're in an async context already
                    future = asyncio.ensure_future(
                        self._runner._delivery.send_to(
                            dt.platform, dt.chat_id, message, dt.thread_id
                        )
                    )
                    # Can't await here in sync handler — schedule it
                    return (
                        f"Message queued for delivery to {target}.\n"
                        f"Use /gateway to check delivery status."
                    )
                else:
                    result = loop.run_until_complete(
                        self._runner._delivery.send_to(
                            dt.platform, dt.chat_id, message, dt.thread_id
                        )
                    )
                    if result.success:
                        return f"✅ Message sent to {target} (id: {result.message_id})"
                    return f"❌ Failed to send: {result.error}"
            except Exception as e:
                return f"Error sending message: {e}"

        return (
            "Gateway is not running. Start it with:\n"
            "  python3 tau-gateway/__main__.py\n\n"
            "Or set platform tokens via environment variables:\n"
            "  TAU_GATEWAY_TELEGRAM_TOKEN=...\n"
            "  TAU_GATEWAY_DISCORD_TOKEN=..."
        )

    def _handle_session_search(
        self,
        query: str,
        source: str | None = None,
        role: str | None = None,
        limit: int = 10,
    ) -> str:
        if not self._state_db:
            return (
                "Session search is not available — no SQLite state store.\n"
                "The state store will be initialized when sessions are recorded."
            )

        if not query or len(query.strip()) < 2:
            return "Error: search query must be at least 2 characters."

        limit = min(max(1, limit or 10), 50)
        source_filter = [source] if source else None
        role_filter = [role] if role else None

        try:
            matches = self._state_db.search_messages(
                query=query,
                source_filter=source_filter,
                role_filter=role_filter,
                limit=limit,
            )
        except Exception as e:
            return f"Search error: {e}"

        if not matches:
            return f"No results found for: \"{query}\""

        # Format results
        parts = [
            f"**Search:** \"{query}\"",
            f"**Results:** {len(matches)}",
            "",
        ]

        for i, m in enumerate(matches, 1):
            snippet = m.get("snippet", "")
            # Clean snippet markers
            snippet = snippet.replace(">>>", "**").replace("<<<", "**")

            session_id = m.get("session_id", "?")[:8]
            role_str = m.get("role", "?")
            source_str = m.get("source", "?")
            model_str = m.get("model", "?")

            parts.append(f"{i}. [{role_str}] session:{session_id} ({source_str}, {model_str})")
            parts.append(f"   {snippet}")

            # Show context if available
            context = m.get("context", [])
            if context and len(context) > 1:
                parts.append("   _context:_")
                for ctx in context[:3]:
                    preview = (ctx.get("content", "") or "")[:100]
                    parts.append(f"   > [{ctx.get('role', '?')}] {preview}")
            parts.append("")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Slash command handlers
    # ------------------------------------------------------------------

    def _handle_gateway_status(self, context: ExtensionContext) -> None:
        """Show gateway status."""
        lines = ["[bold cyan]🤖 Gateway Status[/bold cyan]", ""]

        managed_pid = self._read_managed_pid()
        managed_running = bool(managed_pid and self._is_pid_running(managed_pid))
        if managed_pid and managed_running and not self._looks_like_gateway_pid(managed_pid):
            self._clear_managed_pid()
            managed_pid = None
            managed_running = False
            lines.append("[yellow]○ Managed PID was stale/non-gateway and was cleared[/yellow]")
        elif managed_pid and not managed_running:
            self._clear_managed_pid()

        if managed_running:
            lines.append("[green]● Standalone gateway daemon is running[/green]")
            lines.append(f"  PID: {managed_pid}")
            lines.append(f"  Log: {self._managed_log_path()}")
            lines.append(f"  Source: {self._pid_cmdline(managed_pid) or '(unknown)'}")
        elif self._runner and self._runner._running:
            lines.append("[green]● Gateway is running[/green]")
            adapters = self._runner._adapters
            lines.append(f"  Platforms: {', '.join(adapters.keys())}")
            lines.append(f"  Sessions: {self._runner._sessions.session_count}")
        else:
            lines.append("[yellow]○ Gateway is not running[/yellow]")
            lines.append("  Start with: /gateway-start")
            ep = self._find_gateway_entrypoint()
            if ep is not None:
                lines.append(f"  (or run: python3 {ep})")
            else:
                lines.append("  (set TAU_GATEWAY_ENTRYPOINT=/abs/path/__main__.py)")

        if self._state_db:
            try:
                count = self._state_db.session_count()
                msg_count = self._state_db.message_count()
                lines.append(f"\n  State DB: {count} sessions, {msg_count:,} messages")
            except Exception:
                lines.append("  State DB: available")
        else:
            lines.append("  State DB: not initialized")

        context.print("\n".join(lines))

    def _handle_send_slash(self, args: str, context: ExtensionContext) -> None:
        """Handle /send <target> <message>."""
        parts = args.split(None, 1)
        if len(parts) < 2:
            context.print("[dim]Usage: /send <target> <message>[/dim]")
            context.print("[dim]Example: /send telegram:12345 Hello![/dim]")
            return

        target, message = parts
        result = self._handle_send_message(action="send", target=target, message=message)
        context.print(result)

    def _handle_gateway_setup_slash(self, args: str, context: ExtensionContext) -> None:
        """Handle /gateway-setup <platform>."""
        platform = (args or "").strip().lower() or "telegram"
        if platform != "telegram":
            context.print(f"[dim]Unsupported platform '{platform}'. Currently supported: telegram[/dim]")
            return

        lines = [
            "[bold cyan]Telegram Gateway Setup[/bold cyan]",
            "",
            "1) Install dependencies:",
            "   python3 -m pip install \"python-telegram-bot>=20\" pyyaml",
            "",
            "2) Export env vars:",
            "   export TAU_GATEWAY_TELEGRAM_TOKEN=\"<BOT_TOKEN>\"",
            "   export TAU_GATEWAY_PROVIDER=\"openai\"",
            "   export TAU_GATEWAY_MODEL=\"gpt-4o-mini\"",
            "   export TAU_GATEWAY_WORKSPACE_ROOT=\"/Users/<you>/workspace_for_ai\"",
            "   export OPENAI_API_KEY=\"<YOUR_OPENAI_KEY>\"",
            "",
            "3) Run gateway:",
            "   /gateway-start",
            "   # deterministic entrypoint from loaded package",
            "   # optional override:",
            "   # export TAU_GATEWAY_ENTRYPOINT=/abs/path/__main__.py",
            "",
            "4) Verify in Telegram:",
            "   /start",
            "   /status",
            "   (then send a normal message)",
            "",
            "Optional ~/.tau/gateway.yaml:",
            "platforms:",
            "  telegram:",
            "    platform: telegram",
            "    enabled: true",
            "    token: \"<BOT_TOKEN>\"",
            "    reply_to_mode: always",
            "provider: openai",
            "model: gpt-4o-mini",
            "workspace_root: /Users/<you>/workspace_for_ai",
        ]
        context.print("\n".join(lines))

    def _handle_gateway_start_slash(self, context: ExtensionContext) -> None:
        # Restart semantics: stop all currently running gateway daemons first.
        current_pid = os.getpid()
        stopped = 0
        for pid in self._list_gateway_pids():
            if pid == current_pid:
                continue
            if self._terminate_pid(pid):
                stopped += 1
        self._clear_managed_pid()

        entry = self._find_gateway_entrypoint()
        if entry is None:
            context.print("[red]Could not find gateway entrypoint (__main__.py).[/red]")
            return

        log_path = self._managed_log_path()
        with log_path.open("ab") as logf:
            proc = subprocess.Popen(
                [sys.executable, str(entry)],
                cwd=str(entry.parent),
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
            )

        time.sleep(0.8)
        if proc.poll() is not None:
            context.print(
                "[red]Gateway failed to start.[/red]\n"
                f"[dim]Check log: {log_path}[/dim]"
            )
            return

        self._write_managed_pid(proc.pid)
        context.print(
            f"[green]Started gateway daemon[/green] (pid={proc.pid})\n"
            f"[dim]Stopped existing gateway processes: {stopped}[/dim]\n"
            f"[dim]Entrypoint: {entry}[/dim]\n"
            f"[dim]Log: {log_path}[/dim]"
        )

    def _handle_gateway_stop_slash(self, context: ExtensionContext) -> None:
        pid = self._read_managed_pid()
        if not pid:
            # Fall back: stop any discovered gateway daemons.
            current_pid = os.getpid()
            candidates = [p for p in self._list_gateway_pids() if p != current_pid]
            if not candidates:
                context.print("[yellow]No managed gateway PID found.[/yellow]")
                return
            stopped = 0
            for p in candidates:
                if self._terminate_pid(p):
                    stopped += 1
            context.print(f"[green]Stopped gateway daemons[/green] (count={stopped})")
            return
        if not self._is_pid_running(pid):
            self._clear_managed_pid()
            context.print("[yellow]Managed gateway PID is stale; cleared.[/yellow]")
            return
        if not self._looks_like_gateway_pid(pid):
            self._clear_managed_pid()
            context.print(
                f"[yellow]Managed PID {pid} does not look like gateway; cleared for safety.[/yellow]"
            )
            return

        if self._terminate_pid(pid):
            self._clear_managed_pid()
            context.print(f"[green]Stopped gateway daemon[/green] (pid={pid})")
            return

        context.print(
            f"[red]Gateway did not stop in time[/red] (pid={pid}). "
            "Please stop it manually."
        )

    def _handle_channels_slash(self, args: str, context: ExtensionContext) -> None:
        """Handle /channels [platform]."""
        platform = args.strip() or None
        if self._channel_directory:
            display = self._channel_directory.format_for_display(platform)
            context.print(display)
        else:
            context.print("[dim]No channel directory available. Start the gateway first.[/dim]")


# Module-level instance
EXTENSION = GatewayExtension()
