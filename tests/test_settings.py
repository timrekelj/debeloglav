"""The admin-editable settings in SQLite: storage, validation, defaults, instructions, and the bot
picking up a change on the next turn (with fakes, no network)."""

import pytest
from mistralai import agents

from debeloglav import agent, bot, context, settings, store


@pytest.fixture(autouse=True)
def clean_settings():
    """Each test starts with no saved settings: conftest points all tests at one temp database."""
    with store._connect() as db:
        db.execute("DELETE FROM settings")


# --- Store -----------------------------------------------------------------------------------------


def test_settings_round_trip():
    assert store.get_setting("model") is None
    assert store.all_settings() == {}
    store.set_setting("model", "mistral-medium-latest")
    store.set_setting("reasoning_effort", "high")
    assert store.get_setting("model") == "mistral-medium-latest"
    assert store.get_setting("reasoning_effort") == "high"
    assert store.get_setting("extra_instructions") is None
    assert store.all_settings() == {
        "model": "mistral-medium-latest",
        "reasoning_effort": "high",
    }
    store.set_setting("model", "mistral-large-4")  # overwriting keeps one row
    assert store.get_setting("model") == "mistral-large-4"
    store.set_setting("model", None)  # None deletes: back to the default
    assert store.get_setting("model") is None
    assert store.all_settings() == {"reasoning_effort": "high"}


def test_settings_are_really_in_sqlite():
    store.set_setting("extra_instructions", "Bodi zabaven.")
    with store._connect() as db:  # a fresh connection sees the row
        row = db.execute(
            "SELECT value, updated_at FROM settings WHERE key = 'extra_instructions'"
        ).fetchone()
    assert row is not None and row["value"] == "Bodi zabaven." and row["updated_at"]


# --- Validation and defaults ------------------------------------------------------------------------


def test_without_overrides_the_settings_are_the_defaults():
    assert agent.current_settings() == settings.defaults()
    assert set(settings.defaults()) == set(settings.BY_KEY)
    assert settings.get("extra_instructions") == ""


def test_the_environment_is_never_read(monkeypatch):
    monkeypatch.setenv("MISTRAL_MODEL", "env-model")
    monkeypatch.setenv("CONTEXT_MESSAGES", "4")
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    assert settings.get("model") == "mistral-large-4"  # the built-in defaults
    assert settings.get_int("context_messages") == 10
    assert context.timezone().key == "Europe/Ljubljana"


def test_a_saved_value_wins_until_it_is_reset():
    settings.update("context_messages", "12")
    assert settings.get_int("context_messages") == 12
    assert settings.overrides() == {"context_messages": "12"}
    settings.update("context_messages", None)
    assert settings.get_int("context_messages") == 10


def test_thread_settings_take_only_their_choices():
    assert settings.get("reply_in_threads") == "on"
    assert settings.get_int("thread_archive_hours") == 24
    for key, bad in (("reply_in_threads", "yes"), ("thread_archive_hours", "12")):
        with pytest.raises(ValueError):
            settings.update(key, bad)
    settings.update("thread_archive_hours", "168")
    assert settings.get_int("thread_archive_hours") == 168


def test_numbers_and_time_zones_are_validated():
    for bad in ("abc", "-1", "51", "", "3.5"):
        with pytest.raises(ValueError):
            settings.update("context_messages", bad)
    assert settings.update("context_messages", " 0 ")["context_messages"] == "0"
    with pytest.raises(ValueError):
        settings.update("timezone", "Mars/Olympus")
    settings.update("timezone", "America/New_York")
    assert context.timezone().key == "America/New_York"
    settings.update("timezone", None)


def test_update_setting_accepts_only_valid_values():
    for bad in ("ultra", "", "LOW", "off"):
        with pytest.raises(ValueError):
            agent.update_setting("reasoning_effort", bad)
    for good in agent.REASONING_EFFORTS:
        values = agent.update_setting("reasoning_effort", good)
        assert values["reasoning_effort"] == good
    with pytest.raises(ValueError):
        agent.update_setting("model", "   ")
    with pytest.raises(ValueError):
        agent.update_setting("no-such-setting", "x")
    # nothing invalid was saved: only the last valid reasoning_effort
    assert store.all_settings() == {"reasoning_effort": agent.REASONING_EFFORTS[-1]}


def test_update_setting_none_resets_to_the_default():
    agent.update_setting("reasoning_effort", "high")
    assert store.get_setting("reasoning_effort") == "high"
    values = agent.update_setting("reasoning_effort", None)
    assert store.get_setting("reasoning_effort") is None
    assert values["reasoning_effort"] == settings.defaults()["reasoning_effort"]


def test_current_settings_ignore_unknown_saved_keys():
    store.set_setting("bogus", "x")
    store.set_setting("video_frames", "999")  # saved under older limits
    assert set(agent.current_settings()) == set(settings.BY_KEY)
    assert (
        agent.current_settings()["video_frames"] == settings.defaults()["video_frames"]
    )


def test_settings_version_changes_only_with_the_settings():
    before = agent.settings_version()
    assert agent.settings_version() == before  # no change, no new version
    agent.update_setting("extra_instructions", "Novi slog.")
    changed = agent.settings_version()
    assert changed != before
    agent.update_setting("extra_instructions", None)
    assert agent.settings_version() == before  # reset: back to the same version


# --- Instructions and the built agent ---------------------------------------------------------------


def test_effective_instructions_without_extra():
    assert agent.effective_instructions() == agent.INSTRUCTIONS


def test_effective_instructions_with_extra():
    agent.update_setting("extra_instructions", "Vedno odgovori na kratko.")
    assert agent.effective_instructions() == (
        f"{agent.INSTRUCTIONS}\n\n{agent.EXTRA_INSTRUCTIONS_HEADING}\nVedno odgovori na kratko."
    )
    agent.update_setting("extra_instructions", None)
    assert agent.effective_instructions() == agent.INSTRUCTIONS


def test_build_agent_uses_the_effective_settings():
    agent.update_setting("model", "mistral-medium-latest")
    agent.update_setting("reasoning_effort", "high")
    agent.update_setting("extra_instructions", "Piši aforizme.")
    built = agent.build_agent()
    assert built.model == "mistral-medium-latest"
    assert built.reasoning_effort == "high"
    assert built.instructions == agent.effective_instructions()
    agent.update_setting("reasoning_effort", "none")  # "none" means no reasoning effort
    built = agent.build_agent()
    assert built.reasoning_effort is None
    assert built.model == "mistral-medium-latest"


# --- The bot picks up a change on the next turn ------------------------------------------------------


class FakeSession:
    def __init__(self, session_id: str):
        self.id = session_id
        self.ran: list[object] = []

    async def run(self, content, on_event=None):
        self.ran.append(content)
        return agents.Completed(
            session_id=self.id, turn_id="t1", cursor="c1", text="ok", output=None
        )


class FakeAgent:
    def __init__(self):
        self.started: list[FakeSession] = []
        self.bound: list[str] = []

    async def start_session(self) -> FakeSession:
        session = FakeSession(f"fake-{len(self.started)}")
        self.started.append(session)
        return session

    def session(self, session_id: str) -> FakeSession:
        self.bound.append(session_id)
        return FakeSession(session_id)


async def test_run_turn_rebuilds_the_agent_when_a_setting_changes(monkeypatch):
    built: list[FakeAgent] = []

    def fake_build_agent() -> FakeAgent:
        built.append(FakeAgent())
        return built[-1]

    monkeypatch.setattr(bot, "build_agent", fake_build_agent)
    client = bot.Debeloglav()
    assert client.agent is built[0] and len(built) == 1
    assert client.apply_settings() is False  # nothing changed yet
    request = context.Request(
        scope="guild:settings", channel_id=10, user_id=1, user_name="Tim (@tim)"
    )
    content = [{"type": "text", "text": "Živjo"}]

    session_id, _state = await client.run_turn(client.agent, None, request, content)
    assert session_id == "fake-0" and built[0].started[0].ran == [content]
    assert len(built) == 1  # unchanged settings: no rebuild

    agent.update_setting("reasoning_effort", "high")  # as the admin dashboard would
    session_id, _state = await client.run_turn(client.agent, None, request, content)
    assert len(built) == 2  # the changed setting rebuilt the agent for this turn
    assert client.agent is built[1]
    assert session_id == "fake-0" and built[1].started[0].ran == [content]
    assert built[0].started[0].ran == [
        content
    ]  # the old agent served only its own turn

    # A reply in the running conversation still works, on the rebuilt agent, by session id.
    agent.update_setting("model", "mistral-medium-latest")
    session_id, _state = await client.run_turn(client.agent, "fake-0", request, content)
    assert len(built) == 3 and client.agent is built[2]
    assert built[2].bound == ["fake-0"]  # the rebuilt agent resumed the old session
    assert client.apply_settings() is False  # no change since the last turn


async def test_a_rejected_setting_does_not_rebuild_the_agent(monkeypatch):
    built: list[FakeAgent] = []
    monkeypatch.setattr(
        bot, "build_agent", lambda: built.append(FakeAgent()) or built[-1]
    )
    client = bot.Debeloglav()
    with pytest.raises(ValueError):
        agent.update_setting("reasoning_effort", "ultra")
    request = context.Request(
        scope="guild:settings", channel_id=10, user_id=1, user_name="Tim (@tim)"
    )
    await client.run_turn(
        client.agent, None, request, [{"type": "text", "text": "Živjo"}]
    )
    assert len(built) == 1  # the invalid value was rejected, so no rebuild
