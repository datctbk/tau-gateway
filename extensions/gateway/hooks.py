"""Lifecycle hook registry.

Discovers and loads event handlers from ~/.tau/hooks/. Handlers can
subscribe to events like gateway:startup, session:start, session:end,
agent:start, agent:end, and command:*.

Supports wildcard patterns (e.g. "session:*" matches all session events).
"""

from __future__ import annotations

import fnmatch
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

TAU_HOME = Path.home() / ".tau"
HOOKS_DIR = TAU_HOME / "hooks"

# Known event names
EVENTS = [
    "gateway:startup",
    "gateway:shutdown",
    "session:start",
    "session:end",
    "session:reset",
    "agent:start",
    "agent:end",
    "agent:error",
    "message:inbound",
    "message:outbound",
    "command:new",
    "command:reset",
]

# Type for hook handlers
HookHandler = Callable[[str, Dict[str, Any]], None]


class HookRegistry:
    """Registry for lifecycle event hooks.

    Hooks are Python files in ~/.tau/hooks/ that expose a ``HOOKS`` dict
    mapping event patterns to handler functions::

        # ~/.tau/hooks/my_hook.py
        def on_session_start(event_name, data):
            print(f"Session started: {data['session_id']}")

        HOOKS = {
            "session:start": on_session_start,
        }

    Wildcard patterns are supported::

        HOOKS = {
            "session:*": handle_all_session_events,
            "*": log_everything,
        }
    """

    def __init__(self, hooks_dir: Path | None = None):
        self._hooks_dir = hooks_dir or HOOKS_DIR
        self._handlers: Dict[str, List[HookHandler]] = {}
        self._loaded_files: list[str] = []

    def discover(self) -> int:
        """Discover and load hook files from the hooks directory.

        Returns the number of handlers registered.
        """
        if not self._hooks_dir.is_dir():
            return 0

        count = 0
        for path in sorted(self._hooks_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                loaded = self._load_hook_file(path)
                count += loaded
                self._loaded_files.append(path.name)
            except Exception as exc:
                logger.warning("Failed to load hook %s: %s", path, exc)

        logger.debug("Loaded %d hook handlers from %d files", count, len(self._loaded_files))
        return count

    def _load_hook_file(self, path: Path) -> int:
        """Load a single hook file. Returns number of handlers registered."""
        module_name = f"_tau_hook_{path.stem}_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return 0

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)

        hooks_dict = getattr(mod, "HOOKS", None)
        if not isinstance(hooks_dict, dict):
            return 0

        count = 0
        for pattern, handler in hooks_dict.items():
            if callable(handler):
                self.register(pattern, handler)
                count += 1

        return count

    def register(self, pattern: str, handler: HookHandler) -> None:
        """Register a handler for an event pattern."""
        if pattern not in self._handlers:
            self._handlers[pattern] = []
        self._handlers[pattern].append(handler)

    def emit(self, event_name: str, data: Dict[str, Any] | None = None) -> int:
        """Emit an event. Returns number of handlers called.

        Matches event_name against all registered patterns (including wildcards).
        Handlers are called synchronously — exceptions are caught and logged.
        """
        if data is None:
            data = {}

        called = 0
        for pattern, handlers in self._handlers.items():
            if fnmatch.fnmatch(event_name, pattern):
                for handler in handlers:
                    try:
                        handler(event_name, data)
                        called += 1
                    except Exception as exc:
                        logger.warning(
                            "Hook handler for %r raised on event %r: %s",
                            pattern,
                            event_name,
                            exc,
                        )

        return called

    @property
    def handler_count(self) -> int:
        """Total number of registered handlers."""
        return sum(len(h) for h in self._handlers.values())

    @property
    def loaded_files(self) -> list[str]:
        """List of loaded hook file names."""
        return list(self._loaded_files)

    def registered_patterns(self) -> list[str]:
        """List of registered event patterns."""
        return list(self._handlers.keys())
