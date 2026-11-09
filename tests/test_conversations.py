"""The message-to-session map in SQLite, and the Conversations that read and write it."""

import asyncio
import os
import shutil
import time
from datetime import UTC, datetime, timedelta

import pytest
from mistralai import agents

from debeloglav import bot, store


@pytest.fixture(autouse=True)
def clean_conversations():
    """Each test starts with an empty map: conftest points all tests at one temp database."""
    with store._connect() as db:
        db.execute("DELETE FROM conversations")


def age(session_id: str, **kwargs) -> None:
    """Set a session's last use into the past, as if the conversation had gone quiet."""
    with store._connect() as db:
        db.execute(
            "UPDATE conversations SET last_used = ? WHERE session_id = ?",
            ((datetime.now(UTC) - timedelta(**kwargs)).isoformat(), session_id),
        )


def test_link_and_look_up_messages():
    store.link_messages("link-s1", 101, 102)
    assert store.session_of(101) == "link-s1"
    assert store.session_of(102) == "link-s1"
    assert store.session_of(999) is None
    store.link_messages("link-s2", 102)  # a message can move to another session
    assert store.session_of(102) == "link-s2"
    store.delete_session_rows("link-s1")
    assert store.session_of(101) is None
    assert store.session_of(102) == "link-s2"  # the other session is untouched


def cutoff_now() -> str:
    """A cutoff a second back, so a session used just now is not 'before' it."""
    return (datetime.now(UTC) - timedelta(seconds=1)).isoformat()


def test_link_marks_the_session_used_now():
    store.link_messages("touch-s1", 201)
    age("touch-s1", hours=7)
    assert store.sessions_idle_since(cutoff_now()) == ["touch-s1"]
    store.link_messages("touch-s1", 202)  # using the session again makes it fresh
    assert store.sessions_idle_since(cutoff_now()) == []


def test_sessions_idle_since_lists_only_the_old_ones():
    store.link_messages("idle-fresh", 301)
    store.link_messages("idle-old", 302)
    age("idle-old", hours=7)
    assert store.sessions_idle_since(cutoff_now()) == ["idle-old"]
    eight_hours_ago = (datetime.now(UTC) - timedelta(hours=8)).isoformat()
    assert store.sessions_idle_since(eight_hours_ago) == []


def test_conversations_get_and_link():
    conversations = bot.Conversations()
    assert conversations.get(None) is None
    assert conversations.get(401) is None
    conversations.link("conv-s1", 401, 402)
    assert conversations.get(401) == "conv-s1"
    assert conversations.get(402) == "conv-s1"


def test_conversations_idle_forgets_old_sessions():
    conversations = bot.Conversations()
    conversations.link("conv-fresh", 501)
    conversations.link("conv-old", 502)
    age("conv-old", hours=7)
    assert conversations.idle() == ["conv-old"]
    assert conversations.get(502) is None  # its messages no longer point anywhere
    assert conversations.get(501) == "conv-fresh"
    assert conversations.idle() == []  # already forgotten


def test_conversations_idle_keeps_a_locked_session():
    conversations = bot.Conversations()
    conversations.link("conv-busy", 601)
    age("conv-busy", hours=7)
    lock = conversations.lock("conv-busy")
    asyncio.run(lock.acquire())  # a turn is running in that session
    try:
        assert conversations.idle() == []
        assert conversations.get(601) == "conv-busy"
    finally:
        lock.release()
    assert conversations.idle() == ["conv-busy"]


def test_delete_journal_removes_only_a_plain_session_directory():
    journal = bot.SESSIONS_DIR / "journal-s1"
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "session.json").write_text("{}")
    keep = bot.SESSIONS_DIR.parent / "keep-me"
    keep.mkdir(exist_ok=True)
    bot.delete_journal("journal-s1")
    assert not journal.exists()
    bot.delete_journal("../keep-me")  # a session id that would escape SESSIONS_DIR
    bot.delete_journal("")
    assert keep.exists()
    keep.rmdir()


async def test_end_session_forgets_rows_and_journal():
    client = bot.Debeloglav()
    client.conversations.link("end-s1", 701)
    journal = bot.SESSIONS_DIR / "end-s1"
    journal.mkdir(parents=True, exist_ok=True)
    async with agents.local_runtime():
        await client.end_session("end-s1")
    assert client.conversations.get(701) is None
    assert not journal.exists()


async def test_prune_orphan_journals_deletes_only_old_unknown_dirs():
    client = bot.Debeloglav()
    sessions = bot.SESSIONS_DIR
    sessions.mkdir(parents=True, exist_ok=True)
    old_unknown = sessions / "old-unknown"
    fresh_unknown = sessions / "fresh-unknown"
    old_known = sessions / "old-known"
    locked_unknown = sessions / "locked-unknown"
    for directory in (old_unknown, fresh_unknown, old_known, locked_unknown):
        directory.mkdir(exist_ok=True)
        (directory / "session.json").write_text("{}")
    client.conversations.link("old-known", 801)  # the table knows this session
    old = time.time() - bot.session_idle().total_seconds() - 60
    for directory in (old_unknown, old_known, locked_unknown):
        os.utime(directory, (old, old))  # as if last touched over 6 hours ago
    lock = client.conversations.lock("locked-unknown")
    await lock.acquire()  # a turn is running in that session
    try:
        client.prune_orphan_journals()
        assert not old_unknown.exists()  # old and unknown: its journal is deleted
        assert fresh_unknown.exists()  # too fresh to touch
        assert old_known.exists()  # still known to the table
        assert locked_unknown.exists()  # locked: a turn may be running in it
    finally:
        lock.release()
    client.prune_orphan_journals()
    assert not locked_unknown.exists()  # unlocked now, so it goes too
    shutil.rmtree(sessions, ignore_errors=True)
