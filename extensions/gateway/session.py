"""Gateway session management.

Maps inbound messages to agent sessions using a deterministic session key
derived from (platform, chat_id, thread_id). Handles session creation,
reset policy evaluation, and context prompt generation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import SessionResetPolicy, ResetMode
from .types import SessionSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session Entry
# ---------------------------------------------------------------------------

@dataclass
class SessionEntry:
    """A live gateway session — tracks the mapping from chat → agent session."""
    session_key: str
    session_id: str
    source: SessionSource
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def touch(self) -> None:
        """Update the last-active timestamp."""
        self.updated_at = time.time()
        self.message_count += 1

    @property
    def idle_minutes(self) -> float:
        return (time.time() - self.updated_at) / 60.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "source": self.source.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }


# ---------------------------------------------------------------------------
# Session Store
# ---------------------------------------------------------------------------

class SessionStore:
    """In-memory session store for the gateway.

    Maps session keys (platform:chat_id[:thread_id]) to SessionEntry
    objects. Handles creation, lookup, reset policy, and pruning.
    """

    def __init__(self, reset_policy: SessionResetPolicy | None = None):
        self._sessions: Dict[str, SessionEntry] = {}
        self._reset_policy = reset_policy or SessionResetPolicy()

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def get_or_create(
        self,
        source: SessionSource,
        session_id_factory: Any = None,
    ) -> tuple[SessionEntry, bool]:
        """Get an existing session or create a new one.

        Returns (session_entry, is_new).

        If the reset policy triggers, the old session is ended and a new
        one is created.
        """
        key = source.session_key()

        existing = self._sessions.get(key)
        if existing is not None:
            if self._should_reset(existing):
                logger.info(
                    "Resetting session %s (key=%s) due to reset policy",
                    existing.session_id[:8],
                    key,
                )
                del self._sessions[key]
            else:
                existing.touch()
                return existing, False

        # Create new session
        import uuid
        session_id = str(uuid.uuid4()) if session_id_factory is None else session_id_factory()

        entry = SessionEntry(
            session_key=key,
            session_id=session_id,
            source=source,
        )
        self._sessions[key] = entry
        logger.debug("Created new session %s for %s", session_id[:8], source.display())
        return entry, True

    def get(self, key: str) -> Optional[SessionEntry]:
        """Get a session by key."""
        return self._sessions.get(key)

    def get_by_id(self, session_id: str) -> Optional[SessionEntry]:
        """Find a session by its session_id."""
        for entry in self._sessions.values():
            if entry.session_id == session_id:
                return entry
        return None

    def reset(self, key: str) -> Optional[SessionEntry]:
        """Remove a session by key. Returns the removed entry or None."""
        return self._sessions.pop(key, None)

    def reset_by_source(self, source: SessionSource) -> Optional[SessionEntry]:
        """Remove a session for a given source."""
        return self.reset(source.session_key())

    def list_sessions(self) -> List[SessionEntry]:
        """List all active sessions, most recently active first."""
        return sorted(
            self._sessions.values(),
            key=lambda s: s.updated_at,
            reverse=True,
        )

    # ── Reset policy ──

    def _should_reset(self, entry: SessionEntry) -> bool:
        """Evaluate the reset policy for an existing session."""
        mode = self._reset_policy.mode

        if mode == ResetMode.NONE:
            return False

        if mode in (ResetMode.DAILY, ResetMode.BOTH):
            if self._crossed_daily_boundary(entry):
                return True

        if mode in (ResetMode.IDLE, ResetMode.BOTH):
            if entry.idle_minutes >= self._reset_policy.idle_minutes:
                return True

        return False

    def _crossed_daily_boundary(self, entry: SessionEntry) -> bool:
        """Check if a daily reset boundary has been crossed since last activity."""
        now = datetime.now(timezone.utc)
        last_active = datetime.fromtimestamp(entry.updated_at, tz=timezone.utc)

        # If last activity was on a different day, and we've passed the reset hour
        if last_active.date() < now.date() and now.hour >= self._reset_policy.at_hour:
            return True

        return False


# ---------------------------------------------------------------------------
# Context prompt helpers
# ---------------------------------------------------------------------------

def build_session_context_prompt(
    source: SessionSource,
    connected_platforms: list[str],
    home_channels: list[dict[str, str]] | None = None,
) -> str:
    """Build a system prompt fragment describing the current session context.

    Tells the agent where messages are coming from, what platforms are
    connected, and what messaging capabilities are available.
    """
    lines = [
        "## Session Context",
        "",
        f"- **Message source:** {source.display()}",
        f"- **Platform:** {source.platform}",
    ]
    if source.chat_name:
        lines.append(f"- **Chat:** {source.chat_name}")
    if source.chat_type:
        lines.append(f"- **Chat type:** {source.chat_type}")
    if source.user_name:
        lines.append(f"- **User:** {source.user_name}")
    if source.thread_id:
        lines.append(f"- **Thread:** {source.thread_id}")

    lines.append("")
    lines.append(f"**Connected platforms:** {', '.join(connected_platforms)}")

    if home_channels:
        lines.append("")
        lines.append("**Home channels** (you can proactively post here):")
        for hc in home_channels:
            name = hc.get("chat_name", hc.get("chat_id", ""))
            lines.append(f"  - {hc.get('platform', '?')}:{name}")

    lines.append("")
    lines.append(
        "You can use the `send_message` tool to send messages to any "
        "connected platform. Use the `session_search` tool to search "
        "across all past sessions."
    )

    return "\n".join(lines)
