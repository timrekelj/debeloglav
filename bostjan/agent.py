"""Boštjan's brain: a Mistral agent that researches on the web before it answers."""

import os

from mistralai import agents
from mistralai.client import Mistral
from mistralai.client import models as api_models
from pydantic import BaseModel, Field

MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-4")

client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])


class Source(BaseModel):
    title: str
    url: str


class Reply(BaseModel):
    answer: str = Field(
        description="The reply to post in Discord, in Discord markdown, without a sources list."
    )
    sources: list[Source] = Field(
        default_factory=list,
        description="The web pages from web_search results that back the answer, most relevant first. Empty if you did not search.",
    )


@agents.tool(model_access="direct")
async def web_search(query: str) -> dict[str, object]:
    """Search the web for current or factual information. Returns a summary of what was found and its sources."""
    response = await client.beta.conversations.start_async(
        model=MODEL,
        inputs=query,
        tools=[{"type": "web_search"}],
        store=False,
    )
    text: list[str] = []
    sources: dict[str, Source] = {}
    for entry in response.outputs:
        if not isinstance(entry, api_models.MessageOutputEntry):
            continue
        if isinstance(entry.content, str):
            text.append(entry.content)
            continue
        for chunk in entry.content:
            if isinstance(chunk, api_models.TextChunk):
                text.append(chunk.text)
            elif isinstance(chunk, api_models.ToolReferenceChunk) and chunk.url:
                sources.setdefault(chunk.url, Source(title=chunk.title, url=chunk.url))
    return {
        "answer": "".join(text).strip(),
        "sources": [source.model_dump() for source in sources.values()],
    }


INSTRUCTIONS = """You are Boštjan, a witty, sharp and helpful assistant living in a Discord server, like Grok on X.
People summon you by tagging you in a message.

How to behave:
- Answer the message that tagged you. Use the rest of the conversation only as context.
- If the tagged message is a reply with little or no question (for example "is this true?" or just a tag), fact-check or explain the message it replies to.
- Use web_search whenever the question involves facts, news, current events, prices, people, releases, statistics or anything you are not sure about. Search more than once when one search is not enough. Verify claims before you confirm them.
- Only cite sources that web_search returned. Never invent sources or URLs.
- Be direct and concise: a few short paragraphs at most, unless someone asks for detail. Light humour is welcome, but correctness comes first.
- If a claim is false or misleading, say so plainly and explain why.
- Reply in the language of the person who tagged you (often Slovenian or English).
- Never ping @everyone or @here."""


def build_agent() -> agents.Agent:
    return agents.Agent(
        name="bostjan",
        model=MODEL,
        instructions=INSTRUCTIONS,
        tools=[web_search],
        output_type=Reply,
        builtins=agents.Builtins(
            skills=False, subagents=False, programmatic_tool_calling=False
        ),
        max_iterations=12,
    )
