"""Tests for gateway session store."""

import time
import pytest

from extensions.gateway.config import ResetMode, SessionResetPolicy
from extensions.gateway.session import (
    SessionEntry,
    SessionStore,
    build_session_context_prompt,
)
from extensions.gateway.types import SessionSource


@pytest.fixture
def source():
    return SessionSource(
        platform="telegram",
        chat_id="12345",
        chat_name="Dev Chat",
        user_id="user1",
    )


@pytest.fixture
def store():
    return SessionStore()


class TestSessionStore:
    def test_get_or_create_new(self, store, source):
        entry, is_new = store.get_or_create(source)
        assert is_new is True
        assert entry.session_key == "telegram:12345"
        assert entry.message_count == 0

    def test_get_or_create_existing(self, store, source):
        entry1, _ = store.get_or_create(source)
        entry2, is_new = store.get_or_create(source)
        assert is_new is False
        assert entry1.session_id == entry2.session_id
        assert entry2.message_count == 1  # touch() incremented it

    def test_get_by_key(self, store, source):
        entry, _ = store.get_or_create(source)
        found = store.get(source.session_key())
        assert found is not None
        assert found.session_id == entry.session_id

    def test_get_by_id(self, store, source):
        entry, _ = store.get_or_create(source)
        found = store.get_by_id(entry.session_id)
        assert found is not None
        assert found.session_key == entry.session_key

    def test_reset(self, store, source):
        store.get_or_create(source)
        removed = store.reset(source.session_key())
        assert removed is not None
        assert store.get(source.session_key()) is None

    def test_reset_by_source(self, store, source):
        store.get_or_create(source)
        removed = store.reset_by_source(source)
        assert removed is not None

    def test_list_sessions(self, store):
        src1 = SessionSource(platform="telegram", chat_id="1")
        src2 = SessionSource(platform="discord", chat_id="2")
        store.get_or_create(src1)
        time.sleep(0.01)
        store.get_or_create(src2)

        sessions = store.list_sessions()
        assert len(sessions) == 2
        # Most recent first
        assert sessions[0].source.platform == "discord"

    def test_session_count(self, store, source):
        assert store.session_count == 0
        store.get_or_create(source)
        assert store.session_count == 1

    def test_thread_creates_separate_session(self, store):
        src1 = SessionSource(platform="discord", chat_id="100")
        src2 = SessionSource(platform="discord", chat_id="100", thread_id="t1")
        entry1, _ = store.get_or_create(src1)
        entry2, _ = store.get_or_create(src2)
        assert entry1.session_id != entry2.session_id


class TestResetPolicy:
    def test_no_reset(self, source):
        store = SessionStore(reset_policy=SessionResetPolicy(mode=ResetMode.NONE))
        entry1, _ = store.get_or_create(source)
        # Simulate long idle (set updated_at to 3 hours ago)
        entry1.updated_at = time.time() - 10800
        entry2, is_new = store.get_or_create(source)
        assert is_new is False  # No reset

    def test_idle_reset(self, source):
        store = SessionStore(
            reset_policy=SessionResetPolicy(mode=ResetMode.IDLE, idle_minutes=30)
        )
        entry1, _ = store.get_or_create(source)
        # Simulate 31 minutes idle
        entry1.updated_at = time.time() - (31 * 60)
        entry2, is_new = store.get_or_create(source)
        assert is_new is True
        assert entry2.session_id != entry1.session_id

    def test_idle_no_reset_if_recent(self, source):
        store = SessionStore(
            reset_policy=SessionResetPolicy(mode=ResetMode.IDLE, idle_minutes=60)
        )
        store.get_or_create(source)
        # Immediately access again
        _, is_new = store.get_or_create(source)
        assert is_new is False


class TestSessionEntry:
    def test_touch(self, source):
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            source=source,
        )
        old_ts = entry.updated_at
        time.sleep(0.01)
        entry.touch()
        assert entry.updated_at > old_ts
        assert entry.message_count == 1

    def test_idle_minutes(self, source):
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            source=source,
        )
        entry.updated_at = time.time() - 600  # 10 minutes ago
        assert 9.9 < entry.idle_minutes < 10.5

    def test_to_dict(self, source):
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            source=source,
        )
        d = entry.to_dict()
        assert d["session_key"] == "test"
        assert d["session_id"] == "s1"
        assert d["source"]["platform"] == "telegram"


class TestContextPrompt:
    def test_basic_prompt(self):
        src = SessionSource(
            platform="telegram",
            chat_id="123",
            chat_name="Dev Chat",
            user_name="alice",
        )
        prompt = build_session_context_prompt(
            source=src,
            connected_platforms=["telegram", "discord"],
        )
        assert "telegram" in prompt
        assert "Dev Chat" in prompt
        assert "alice" in prompt
        assert "send_message" in prompt

    def test_prompt_with_home_channels(self):
        src = SessionSource(platform="telegram", chat_id="123")
        prompt = build_session_context_prompt(
            source=src,
            connected_platforms=["telegram"],
            home_channels=[{"platform": "telegram", "chat_name": "Home"}],
        )
        assert "Home channels" in prompt
