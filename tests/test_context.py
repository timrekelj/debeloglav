"""Which request a session's tools act for: the conversation's, or the one a research job was started by."""

from types import SimpleNamespace

import pytest
from mistralai import agents
from mistralai.agents import testing

from debeloglav import agent, context
from debeloglav.context import Request


class FakeSession:
    """A research lead's session, with the researchers it has started."""

    def __init__(self, session_id: str, *researchers: str) -> None:
        self.id = session_id
        self.researchers = list(researchers)
        self.listed = 0

    async def subagents(self) -> list[SimpleNamespace]:
        self.listed += 1
        return [SimpleNamespace(id=researcher) for researcher in self.researchers]


def ctx(session_id: str) -> agents.ToolCallbackContext:
    return SimpleNamespace(session=SimpleNamespace(id=session_id))


def request(name: str = "Tim (@tim)") -> Request:
    return Request(scope="guild:1", channel_id=10, user_id=7, user_name=name)


@pytest.fixture(autouse=True)
def clean():
    yield
    for registry in (context._requests, context._research, context._researchers):
        registry.clear()


async def test_a_conversation_acts_for_its_request():
    asked = request()
    context.register("s1", asked)
    assert await context.asker(ctx("s1")) is asked


async def test_a_research_lead_and_its_researchers_act_for_the_request():
    asked, other = request(), request("Ana (@ana)")
    lead = FakeSession("lead", "r1")
    context.register_research(lead, asked)
    context.register_research(FakeSession("lead2", "x1"), other)
    assert await context.asker(ctx("lead")) is asked
    assert lead.listed == 0  # the lead is known without asking
    lead.researchers.append("r2")  # started later
    assert await context.asker(ctx("r2")) is asked
    assert await context.asker(ctx("r1")) is asked
    assert await context.asker(ctx("x1")) is other
    assert lead.listed == 1  # found once, then remembered
    assert asked.session_id is None  # still the conversation's to set
    with pytest.raises(agents.ToolError):
        context.current(ctx("lead"))  # the other tools stay out of research


async def test_a_finished_research_is_forgotten():
    context.register_research(FakeSession("lead", "r1"), request())
    await context.asker(ctx("r1"))
    context.forget_research("lead")
    for session_id in ("lead", "r1", "stranger"):
        with pytest.raises(agents.ToolError, match="Discord conversation"):
            await context.asker(ctx(session_id))


async def test_a_lead_that_cannot_list_its_researchers_is_skipped():
    class Broken(FakeSession):
        async def subagents(self):
            raise RuntimeError("gone")

    asked = request()
    context.register_research(Broken("broken"), request("Ana (@ana)"))
    context.register_research(FakeSession("lead", "r1"), asked)
    assert await context.asker(ctx("r1")) is asked


async def test_the_researchers_search_for_the_asker(monkeypatch):
    seen = []

    async def search(request: Request, **kwargs: object) -> dict[str, object]:
        seen.append((request.user_name, kwargs["content"]))
        return {"messages": [], "total": 0}

    monkeypatch.setattr(context, "search_messages", search)
    context.register_research(FakeSession("lead", "r1"), request())
    await agent.search_discord_messages.__wrapped__(ctx("r1"), content="pizza")
    assert seen == [("Tim (@tim)", "pizza")]


async def test_the_research_lead_is_offered_the_search(monkeypatch):
    seen = []

    async def search(request: Request, **kwargs: object) -> dict[str, object]:
        seen.append((request.user_name, kwargs["content"]))
        return {"messages": [], "total": 0}

    monkeypatch.setattr(context, "search_messages", search)
    model = testing.ScriptedModel(  # fails if the tool it calls is not offered
        [testing.call("search_discord_messages", content="pizza"), testing.reply("ni")]
    )
    async with agents.local_runtime():
        session = await agent.build_research_agent().override(model=model).start_session()
        context.register_research(session, request())
        state = await session.run("Kdo je omenil pizzo?")
    assert state.text == "ni"
    assert seen == [("Tim (@tim)", "pizza")]
