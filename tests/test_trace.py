"""A request's trace: the stages it times and the steps it records from the Agents SDK's events."""

import asyncio

from mistralai import agents

from debeloglav import bot, context, dashboard, store
from debeloglav import trace as trace_module
from debeloglav.trace import MAX_STEPS, Trace


def events(session_id: str = "s1", turn_id: str = "t1") -> list[object]:
    ids = {"session_id": session_id, "turn_id": turn_id}
    call = agents.ToolCallEntry(
        id="h1",
        turn_id=turn_id,
        tool_call_id="c1",
        name="python.web_search",
        arguments={"query": "vreme Ljubljana"},
    )
    result = agents.ToolResultEntry(
        id="h2",
        turn_id=turn_id,
        tool_call_id="c1",
        name="python.web_search",
        content=(),
        structured_content={"answer": "Sončno."},
        is_error=False,
    )
    message = agents.MessageEntry(
        id="h3", turn_id=turn_id, role="assistant", content=()
    )
    return [
        agents.TurnStarted(id="e1", **ids),
        agents.EntryCompleted(id="e2", entry=call, **ids),
        agents.EntryCompleted(id="e3", entry=result, **ids),
        agents.EntryAdded(id="e4", entry=message, **ids),
        agents.EntryCompleted(id="e5", entry=message, **ids),
    ]


def test_trace_records_the_turn_steps_in_order():
    trace = Trace()
    for event in events():
        trace.on_event(event)
    kinds = [step["kind"] for step in trace.steps]
    assert kinds == [
        "turn_started",
        "tool_call",
        "tool_result",
        "model_writing",
        "model_message",
    ]
    call, result = trace.steps[1], trace.steps[2]
    assert call["name"] == "python.web_search"
    assert "vreme Ljubljana" in call["detail"]
    assert "Sončno." in result["detail"] and "error" not in result
    assert all(isinstance(step["at_ms"], int) for step in trace.steps)


def test_trace_never_raises_and_caps_its_steps():
    trace = Trace()
    trace.on_event(object())  # an event it doesn't know is ignored
    for _ in range(MAX_STEPS + 10):
        trace.step("busy_wait", detail="x" * 5000)
    assert len(trace.steps) == MAX_STEPS
    assert len(trace.steps[0]["detail"]) <= 600


def test_stages_add_up():
    trace = Trace()
    with trace.stage("context"):
        pass
    with trace.stage("context"):
        pass
    assert set(trace.as_dict()["stages"]) == {"context"}


class EventSession:
    id = "trace-s1"

    async def run(self, content, on_event=None):
        for event in events(self.id):
            on_event(event)
        return agents.Completed(
            session_id=self.id,
            turn_id="t1",
            cursor="c",
            text="ok",
            output="ok",
            tool_calls=(),
        )


class EventAgent:
    model = "mistral-large-4"
    reasoning_effort = "low"

    async def start_session(self):
        return EventSession()


async def test_run_turn_fills_the_trace(monkeypatch):
    monkeypatch.setattr(bot, "build_agent", EventAgent)
    client = bot.Debeloglav()
    request = context.Request(
        scope="guild:t", channel_id=1, user_id=1, user_name="Tim (@tim)"
    )
    trace = Trace()
    await client.run_turn(
        client.agent, None, request, [{"type": "text", "text": "Hej"}], trace=trace
    )
    data = trace.as_dict()
    assert data["model"] == "mistral-large-4" and data["reasoning_effort"] == "low"
    assert data["state"] == "Completed"
    assert {"session_start", "lock_wait", "turn"} <= set(data["stages"])
    assert [s["kind"] for s in data["steps"]][1:3] == ["tool_call", "tool_result"]


class ChildSession:
    def __init__(self, id, finishes):
        self.id = id
        self.finishes = finishes

    async def iter_events(self):
        for event in events(self.id)[:2]:
            yield event
        if self.finishes:
            yield agents.TurnEnded(
                id="e9",
                session_id=self.id,
                turn_id="t1",
                state=agents.Completed(
                    session_id=self.id,
                    turn_id="t1",
                    cursor="c",
                    text="ok",
                    output="ok",
                    tool_calls=(),
                ),
            )
        await asyncio.Event().wait()  # follows the session until it ends


class LeadSession:
    def __init__(self):
        self.children = []

    async def subagents(self):
        return list(reversed(self.children))  # newest first


async def test_trace_follows_the_subagents(monkeypatch):
    monkeypatch.setattr(trace_module, "SUBAGENT_POLL_SECONDS", 0.01)
    trace = Trace()
    lead = LeadSession()
    async with trace.following_subagents(lead):
        lead.children += [
            ChildSession("a", finishes=True),
            ChildSession("b", finishes=False),
        ]
        for _ in range(20):
            await asyncio.sleep(0.01)
    by_agent = {}
    for step in trace.steps:
        by_agent.setdefault(step.get("agent") or step.get("name"), []).append(
            step["kind"]
        )
    assert by_agent["researcher 1"][:3] == [
        "subagent_started",
        "turn_started",
        "tool_call",
    ]
    assert by_agent["researcher 1"][-1] == "turn_ended"
    assert by_agent["researcher 2"][-1] == "subagent_unfinished"


def test_tool_timings_pair_calls_per_agent():
    steps = [
        {
            "at_ms": 0,
            "kind": "tool_call",
            "name": "web_search",
            "agent": "researcher 1",
        },
        {
            "at_ms": 10,
            "kind": "tool_call",
            "name": "web_search",
            "agent": "researcher 2",
        },
        {
            "at_ms": 15,
            "kind": "tool_result",
            "name": "web_search",
            "agent": "researcher 2",
        },
        {
            "at_ms": 100,
            "kind": "tool_result",
            "name": "web_search",
            "agent": "researcher 1",
        },
    ]
    assert dashboard.tool_timings({"steps": steps}) == [
        ("web_search", 5),
        ("web_search", 100),
    ]


def test_the_trace_is_stored_with_the_activity():
    store.log_activity(
        kind="reply", scope="guild:t", trace={"stages": {"turn": 5}, "steps": []}
    )
    (row,) = [a for a in store.recent_activity(5) if a.scope == "guild:t"]
    assert row.trace == {"stages": {"turn": 5}, "steps": []}
    store.log_activity(kind="reply", scope="guild:t2")
    (row,) = [a for a in store.recent_activity(5) if a.scope == "guild:t2"]
    assert row.trace == {}
