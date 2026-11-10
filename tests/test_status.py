"""The request_status tool: this server's recent requests, their errors, and what it hides."""

from types import SimpleNamespace

import pytest
from mistralai import agents
from test_search import ASKER, GUILD_ID, FakeChannel, FakeGuild, person

from debeloglav import status, store
from debeloglav.context import Request

SCOPE = f"guild:{GUILD_ID}"
PUBLIC, SECRET = 10, 20


def bot():
    guild = FakeGuild(
        [FakeChannel(PUBLIC, "splosno"), FakeChannel(SECRET, "admini", public=False)],
        [person(ASKER, "tim")],
    )
    return SimpleNamespace(get_guild=lambda guild_id: guild)


def request(**kwargs):
    return Request(
        scope=SCOPE, channel_id=PUBLIC, user_id=ASKER, user_name="Tim (@tim)", **kwargs
    )


@pytest.fixture(autouse=True)
def activity():
    store.log_activity(
        kind="reply", scope="guild:other", channel_id=PUBLIC, question="tuje"
    )
    store.log_activity(
        kind="research",
        scope=SCOPE,
        channel_id=PUBLIC,
        user_name="Karen (@thisisboxi)",
        question="koga volim",
        ok=False,
        error="TimeoutError: the research ran longer than the 20 min limit",
        duration_ms=1_201_000,
        trace={
            "stages": {"turn": 1_200_000},
            "steps": [
                {"at_ms": 1, "kind": "subagent_started", "name": "researcher 1"},
                {"at_ms": 2, "kind": "subagent_started", "name": "researcher 2"},
                {
                    "at_ms": 3,
                    "kind": "tool_result",
                    "name": "python.web_search",
                    "agent": "researcher 2",
                    "detail": "HTTP 500",
                    "error": True,
                },
                {
                    "at_ms": 4,
                    "kind": "subagent_unfinished",
                    "name": "researcher 2",
                    "agent": "researcher 2",
                    "error": True,
                },
            ],
        },
    )
    store.log_activity(
        kind="reply",
        scope=SCOPE,
        channel_id=SECRET,
        user_name="Tim (@tim)",
        question="skrivnost",
        reply="psst",
        duration_ms=3000,
    )
    store.log_activity(
        kind="reply",
        scope=SCOPE,
        channel_id=PUBLIC,
        user_name="Tim (@tim)",
        question="rip",
        reply="RIP",
        duration_ms=2900,
    )
    yield
    with store._connect() as db:
        db.execute("DELETE FROM activity")


async def test_lists_this_servers_requests_newest_first():
    result = await status.request_status(bot(), request(), limit=10)
    rows = result["requests"]
    assert [r["kind"] for r in rows] == ["reply", "reply", "research"]
    assert rows[0]["question"] == "rip" and rows[0]["reply"] == "RIP"
    assert rows[0]["status"] == "ok" and rows[0]["seconds"] == 2.9
    assert all("tuje" != r.get("question") for r in rows)  # another server's row


async def test_hides_a_private_channels_content():
    rows = (await status.request_status(bot(), request(), limit=10))["requests"]
    secret = rows[1]
    assert secret["channel"] == "a private channel"
    assert "question" not in secret and "reply" not in secret
    # asked privately (e.g. ephemerally) it may show it
    rows = (await status.request_status(bot(), request(private=True), limit=10))[
        "requests"
    ]
    assert rows[1]["question"] == "skrivnost" and rows[1]["channel"] == "#admini"


async def test_explains_a_failed_research():
    (row,) = (
        await status.request_status(bot(), request(), kind="research", failed_only=True)
    )["requests"]
    assert row["status"] == "failed" and row["error"].startswith(
        "TimeoutError: the research"
    )
    details = row["details"]
    assert details["researchers"] == 2
    assert details["researchers_still_running_at_the_end"] == ["researcher 2"]
    assert details["failed_tool_calls"] == [
        {"agent": "researcher 2", "tool": "web_search", "error": "HTTP 500"}
    ]
    assert details["seconds_per_stage"] == {"turn": 1200.0}


async def test_filters_by_person_and_checks_kind():
    rows = (await status.request_status(bot(), request(), asked_by="@thisisboxi"))[
        "requests"
    ]
    assert [r["asked_by"] for r in rows] == ["Karen (@thisisboxi)"]
    with pytest.raises(agents.ToolError):
        await status.request_status(bot(), request(), kind="nope")
