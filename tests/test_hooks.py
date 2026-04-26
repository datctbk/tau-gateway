"""Tests for hook registry."""

import pytest
from pathlib import Path

from extensions.gateway.hooks import HookRegistry


@pytest.fixture
def registry():
    return HookRegistry(hooks_dir=Path("/nonexistent"))


class TestHookRegistry:
    def test_register_handler(self, registry):
        calls = []
        registry.register("session:start", lambda e, d: calls.append(e))
        assert registry.handler_count == 1

    def test_emit_event(self, registry):
        calls = []
        registry.register("session:start", lambda e, d: calls.append(e))
        count = registry.emit("session:start", {"session_id": "s1"})
        assert count == 1
        assert calls == ["session:start"]

    def test_emit_no_handlers(self, registry):
        count = registry.emit("unknown:event")
        assert count == 0

    def test_wildcard_matching(self, registry):
        calls = []
        registry.register("session:*", lambda e, d: calls.append(e))
        registry.emit("session:start")
        registry.emit("session:end")
        registry.emit("gateway:startup")
        assert calls == ["session:start", "session:end"]

    def test_catch_all_wildcard(self, registry):
        calls = []
        registry.register("*", lambda e, d: calls.append(e))
        registry.emit("session:start")
        registry.emit("gateway:startup")
        assert len(calls) == 2

    def test_handler_error_caught(self, registry):
        def bad_handler(event, data):
            raise RuntimeError("oops")

        registry.register("test", bad_handler)
        # Should not raise
        count = registry.emit("test")
        assert count == 0  # The handler raised, so it "failed"

    def test_multiple_handlers_same_pattern(self, registry):
        calls = []
        registry.register("test", lambda e, d: calls.append("h1"))
        registry.register("test", lambda e, d: calls.append("h2"))
        count = registry.emit("test")
        assert count == 2
        assert calls == ["h1", "h2"]

    def test_registered_patterns(self, registry):
        registry.register("session:*", lambda e, d: None)
        registry.register("gateway:startup", lambda e, d: None)
        patterns = registry.registered_patterns()
        assert "session:*" in patterns
        assert "gateway:startup" in patterns


class TestDiscovery:
    def test_discover_from_dir(self, tmp_path):
        # Create a hook file
        hook_file = tmp_path / "my_hook.py"
        hook_file.write_text(
            "events = []\n"
            "def on_start(event, data):\n"
            "    events.append(event)\n"
            "\n"
            "HOOKS = {'gateway:startup': on_start}\n"
        )
        registry = HookRegistry(hooks_dir=tmp_path)
        count = registry.discover()
        assert count == 1
        assert "my_hook.py" in registry.loaded_files

    def test_discover_empty_dir(self, tmp_path):
        registry = HookRegistry(hooks_dir=tmp_path)
        count = registry.discover()
        assert count == 0

    def test_skip_underscored_files(self, tmp_path):
        (tmp_path / "_internal.py").write_text("HOOKS = {}")
        registry = HookRegistry(hooks_dir=tmp_path)
        count = registry.discover()
        assert count == 0
