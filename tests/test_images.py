"""The image tools, with the Mistral client mocked: URL parsing, fallbacks, edit acceptance, rendering."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mistralai import agents
from mistralai.client import models as api_models

from debeloglav import agent, bot, context
from debeloglav.context import Found, FoundImage, Request

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SESSION = "ses-test"
SIGNED = "https://signed.example/file-1?se=abc"
OUT = "https://out.example/gen.jpg?se=1"


def tool_entry(arguments, result_url=None, name="image_generation"):
    info = {"result": json.dumps({"url": result_url})} if result_url else {}
    return api_models.ToolExecutionEntry(
        name=name,
        arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        object="entry",
        type="tool.execution",
        created_at=NOW,
        completed_at=NOW,
        agent_id="a",
        model="m",
        id="t1",
        info=info,
    )


def message_entry(content):
    return api_models.MessageOutputEntry(
        role="assistant",
        content=content,
        object="entry",
        type="message.output",
        created_at=NOW,
        agent_id="a",
        model="m",
        id="m1",
    )


def file_chunk(file_id):
    return api_models.ToolFileChunk(
        tool="file", file_id=file_id, file_name="a.png", file_type="image/png"
    )


def response(*outputs):
    return api_models.ConversationResponse(
        conversation_id="c",
        outputs=list(outputs),
        usage=api_models.ConversationUsageInfo(
            prompt_tokens=0, completion_tokens=0, total_tokens=0
        ),
        object="conversation.response",
        guardrails=None,
    )


class FakeConversations:
    def __init__(self):
        self.responses: list[object] = []
        self.calls: list[dict] = []

    async def start_async(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeFiles:
    def __init__(self):
        self.uploads: list[tuple[dict, str | None]] = []
        self.deleted: list[str] = []
        self.downloaded: dict[str, bytes] = {}

    async def upload_async(self, *, file, purpose=None, **kwargs):
        self.uploads.append((file, purpose))
        return SimpleNamespace(id=f"file-{len(self.uploads)}")

    async def get_signed_url_async(self, *, file_id, expiry=24, **kwargs):
        return SimpleNamespace(url=f"https://signed.example/{file_id}?se=abc")

    async def delete_async(self, *, file_id, **kwargs):
        self.deleted.append(file_id)

    async def download_async(self, *, file_id, **kwargs):
        data = self.downloaded.get(file_id, b"FILE-" + file_id.encode())

        async def aread():
            return data

        return SimpleNamespace(aread=aread)


class FakeChat:
    def __init__(self, text="A grey cat on a red sofa, photographed from the side."):
        self.text = text
        self.calls: list[dict] = []

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


@pytest.fixture
def env(monkeypatch):
    """A registered Request plus a mocked Mistral client and image download."""
    conversations = FakeConversations()
    files = FakeFiles()
    chat = FakeChat()
    monkeypatch.setattr(
        agent,
        "client",
        SimpleNamespace(
            beta=SimpleNamespace(conversations=conversations), files=files, chat=chat
        ),
    )

    async def fake_download(urls):
        return [b"IMG:" + url.encode() for url in urls]

    monkeypatch.setattr(agent, "_download_images", fake_download)
    request = Request(
        scope="guild:1", channel_id=1, user_id=2, user_name="Luka (@luka)"
    )
    context.register(SESSION, request)
    yield SimpleNamespace(
        ctx=SimpleNamespace(session=SimpleNamespace(id=SESSION)),
        request=request,
        conversations=conversations,
        files=files,
        chat=chat,
    )
    context.forget(SESSION)


def found_images():
    return [img.data for img in context.take_found(SESSION).images]


# --- generate_image ----------------------------------------------------------------------------


async def test_generate_image_downloads_the_url_from_the_tool_result(env):
    env.conversations.responses.append(response(tool_entry({"prompt": "a cat"}, OUT)))
    result = await agent.generate_image.__wrapped__(env.ctx, "a cat in a cafe")
    assert result == "The image is generated and will be attached to your reply."
    assert found_images() == [b"IMG:" + OUT.encode()]
    assert env.conversations.calls[0]["tools"] == [{"type": "image_generation"}]


async def test_generate_image_falls_back_to_a_tool_file_chunk(env):
    env.conversations.responses.append(response(message_entry([file_chunk("fid-1")])))
    await agent.generate_image.__wrapped__(env.ctx, "a cat")
    assert found_images() == [b"FILE-fid-1"]


async def test_generate_image_falls_back_to_a_markdown_url_in_the_message(env):
    env.conversations.responses.append(
        response(message_entry("Here: ![cat](https://out.example/m.jpg)"))
    )
    await agent.generate_image.__wrapped__(env.ctx, "a cat")
    assert found_images() == [b"IMG:https://out.example/m.jpg"]


async def test_generate_image_without_any_image_fails(env):
    env.conversations.responses.append(response(message_entry("I cannot draw that.")))
    with pytest.raises(agents.ToolError):
        await agent.generate_image.__wrapped__(env.ctx, "a cat")
    assert context.take_found(SESSION).images == []


# --- render ------------------------------------------------------------------------------------


async def test_render_attaches_the_image_bytes_with_a_suffix_by_content():
    answer = bot.Answer(
        text="Tu je slika.",
        found=Found(
            images=[
                FoundImage(data=b"\xff\xd8\xff jpeg"),
                FoundImage(data=b"\x89PNG png"),
            ]
        ),
    )
    text, files = await bot.render(answer)
    assert [f.filename for f in files] == ["debeloglav_1.jpg", "debeloglav_2.png"]
    assert text == "Tu je slika."


