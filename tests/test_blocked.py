from types import SimpleNamespace

import pytest

from debeloglav import blocked


@pytest.fixture(autouse=True)
def blocked_ids(monkeypatch):
    monkeypatch.setattr(blocked, "BLOCKED_CHANNELS", frozenset({100, 200}))


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
