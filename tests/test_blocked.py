from types import SimpleNamespace

import pytest

from debeloglav import blocked


@pytest.fixture(autouse=True)
def blocked_ids(monkeypatch):
    # start each test with 100 and 200 blocked in the cache, as if freshly loaded
    monkeypatch.setattr(blocked, "_db_blocked", {100, 200})
    monkeypatch.setattr(blocked, "_db_loaded", True)


def channel(id, category_id=None):
    return SimpleNamespace(id=id, category_id=category_id)


def test_a_blocked_channel():
    assert blocked.channel(channel(100))


def test_a_channel_in_a_blocked_category():
    assert blocked.channel(channel(1, category_id=200))


def test_a_thread_of_a_blocked_channel():
    assert blocked.channel(SimpleNamespace(id=5, parent=channel(100)))


def test_a_thread_in_a_blocked_category():
    assert blocked.channel(SimpleNamespace(id=5, parent=channel(1, category_id=200)))


def test_other_channels_and_dms_are_read():
    assert not blocked.channel(channel(1, category_id=2))
    assert not blocked.channel(SimpleNamespace(id=5, parent=channel(1)))
    assert not blocked.channel(SimpleNamespace(id=7))
    assert not blocked.channel(None)


def test_a_channel_blocked_in_the_database():
    blocked.block(1, 300)
    assert blocked.channel(channel(300))


def test_a_category_blocked_in_the_database():
    blocked.block(1, 301)
    assert blocked.channel(channel(1, category_id=301))


def test_a_thread_of_a_channel_blocked_in_the_database():
    blocked.block(1, 302)
    assert blocked.channel(SimpleNamespace(id=5, parent=channel(302)))


def test_a_thread_in_a_category_blocked_in_the_database():
    blocked.block(1, 303)
    assert blocked.channel(SimpleNamespace(id=5, parent=channel(1, category_id=303)))


def test_unblocking_a_database_channel():
    blocked.block(1, 304)
    assert blocked.channel(channel(304))
    assert blocked.unblock(1, 304)
    assert not blocked.channel(channel(304))
    assert not blocked.unblock(1, 304)  # already unblocked


def test_unblocking_in_another_server_keeps_the_block():
    blocked.block(2, 305)
    assert not blocked.unblock(1, 305)  # not blocked in guild 1
    assert blocked.channel(channel(305))  # guild 2's block still applies
