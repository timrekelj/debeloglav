"""The image tools, with the Mistral client mocked: URL parsing, fallbacks, edit acceptance, rendering."""

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mistralai import agents
from mistralai.client import models as api_models

from debeloglav import agent, bot, context, settings
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


async def test_render_labels_a_regenerated_image():
    answer = bot.Answer(
        text="Spremenjeno.",
        found=Found(
            sources={"https://x": "X"},
            images=[FoundImage(data=b"\xff\xd8\xff new", regenerated=True)],
        ),
    )
    text, files = await bot.render(answer)
    assert files[0].filename == "debeloglav_1.jpg"
    assert "-# Viri: [X](<https://x>)" in text
    assert text.endswith("-# Slike ni bilo mogoče urediti, zato je ustvarjena na novo.")


async def test_render_does_not_label_a_real_edit():
    answer = bot.Answer(
        text="Spremenjeno.",
        found=Found(images=[FoundImage(data=b"\xff\xd8\xff edited")]),
    )
    text, _files = await bot.render(answer)
    assert "Slike ni bilo" not in text


# --- edit_image ---------------------------------------------------------------------------------


async def test_edit_image_accepts_a_real_edit(env):
    env.request.editable_images = [
        context.EditableImage(
            data=b"\xff\xd8src", label="photo.jpg from Luka K (@luka)"
        )
    ]
    env.conversations.responses.append(
        response(
            tool_entry(
                {"image_url": SIGNED, "prompt": "add a red top hat"},
                "https://out.example/edited.jpg?se=2",
            )
        )
    )
    result = await agent.edit_image.__wrapped__(
        env.ctx, instruction="add a red top hat", image=1
    )
    found = context.take_found(SESSION)
    assert result == "The image is edited and will be attached to your reply."
    assert len(found.images) == 1
    assert found.images[0].regenerated is False
    assert found.images[0].data == b"IMG:https://out.example/edited.jpg?se=2"
    # the image was uploaded for editing, and deleted again afterwards
    assert env.files.uploads == [
        ({"file_name": "image.jpg", "content": b"\xff\xd8src"}, "image_generation")
    ]
    assert env.files.deleted == ["file-1"]
    # the signed URL went to the tool both in the text and as the image_url chunk
    content = env.conversations.calls[0]["inputs"][0]["content"]
    assert SIGNED in content[0]["text"] and content[0]["text"].endswith(
        "add a red top hat"
    )
    assert content[1] == {"type": "image_url", "image_url": SIGNED}


async def test_edit_image_retries_when_the_tool_uses_another_url(env):
    env.request.editable_images = [
        context.EditableImage(data=b"src", label="photo.jpg from Luka K (@luka)")
    ]
    env.conversations.responses.extend(
        [
            response(
                tool_entry(
                    {"image_url": "https://wrong.example/x.jpg", "prompt": "p"},
                    "https://out.example/a.jpg",
                )
            ),
            response(
                tool_entry(
                    {
                        "image_url": "https://signed.example/file-2?se=abc",
                        "prompt": "p",
                    },
                    "https://out.example/b.jpg",
                )
            ),
        ]
    )
    await agent.edit_image.__wrapped__(env.ctx, instruction="add a hat", image=1)
    found = context.take_found(SESSION)
    assert found.images[0].data == b"IMG:https://out.example/b.jpg"
    assert found.images[0].regenerated is False
    assert len(env.files.uploads) == 2  # retried with a fresh upload
    assert env.files.deleted == ["file-1", "file-2"]


async def test_edit_image_falls_back_to_a_labelled_regeneration(env):
    env.request.editable_images = [
        context.EditableImage(
            data=b"\xff\xd8src", label="photo.jpg from Luka K (@luka)"
        )
    ]
    env.conversations.responses.extend(
        [
            # attempt 1: the tool edited some other URL
            response(
                tool_entry(
                    {"image_url": "https://wrong.example/x.jpg", "prompt": "p"},
                    "https://out.example/a.jpg",
                )
            ),
            # attempt 2: no tool call at all
            response(message_entry("I cannot edit this.")),
            # the fallback regeneration
            response(
                tool_entry({"prompt": "regen"}, "https://out.example/new.jpg?se=9")
            ),
        ]
    )
    result = await agent.edit_image.__wrapped__(
        env.ctx, instruction="add a red top hat", image=1
    )
    found = context.take_found(SESSION)
    assert len(found.images) == 1
    assert found.images[0].regenerated is True
    assert found.images[0].data == b"IMG:https://out.example/new.jpg?se=9"
    assert "new version" in result
    # the image was described with the vision model, and the change appended to the prompt
    assert len(env.chat.calls) == 1
    assert env.chat.calls[0]["model"] == settings.get("model")
    prompt = env.conversations.calls[2]["inputs"]
    assert "add a red top hat" in prompt
    assert "A grey cat on a red sofa" in prompt
    assert env.files.deleted == ["file-1", "file-2"]


async def test_edit_image_without_an_image_fails(env):
    with pytest.raises(agents.ToolError):
        await agent.edit_image.__wrapped__(env.ctx, instruction="add a hat")


async def test_edit_image_with_a_bad_number_fails(env):
    env.request.editable_images = [
        context.EditableImage(data=b"src", label="photo.jpg from Luka K (@luka)")
    ]
    with pytest.raises(agents.ToolError):
        await agent.edit_image.__wrapped__(env.ctx, instruction="add a hat", image=2)
    assert context.take_found(SESSION).images == []


# --- build_content / read_attachment ------------------------------------------------------------


class EmptyChannel:
    id = 1

    def history(self, limit=None, before=None):
        async def stream():
            for m in []:
                yield m

        return stream()


def attachment(filename, content_type, data=b"raw"):
    async def read():
        return data

    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        size=len(data),
        url=f"https://cdn/{filename}",
        read=read,
        is_voice_message=lambda: False,
    )


async def test_build_content_lists_the_editable_images(monkeypatch):
    async def fake_image(data, file_name):
        return b"\xff\xd8\xffjpeg"

    monkeypatch.setattr(bot.media, "image", fake_image)
    author = SimpleNamespace(id=2, name="luka", display_name="Luka K")
    message = SimpleNamespace(
        id=10,
        author=author,
        clean_content="dodaj mu klobuk",
        attachments=[attachment("photo.jpg", "image/jpeg")],
        reference=None,
        mentions=[],
        embeds=[],
        poll=None,
        channel=EmptyChannel(),
    )
    request = Request(scope="dm:2", channel_id=1, user_id=2, user_name="Luka (@luka)")
    content = await bot.Debeloglav.build_content(
        SimpleNamespace(user=None), message, request, continuing=False
    )
    assert request.editable_images == [
        context.EditableImage(
            data=b"\xff\xd8\xffjpeg", label="photo.jpg from Luka K (@luka)"
        )
    ]
    assert (
        "Images you can edit with edit_image:\n1: photo.jpg from Luka K (@luka)"
        in content[0]["text"]
    )
    assert content[1] == {
        "type": "image",
        "uri": "data:image/jpeg;base64,"
        + base64.b64encode(b"\xff\xd8\xffjpeg").decode(),
        "mediaType": "image/jpeg",
    }


async def test_video_frames_are_not_editable(monkeypatch):
    async def fake_frames(data, file_name):
        return ["data:image/jpeg;base64,AAA"], b"mp3"

    async def fake_transcribe(data, file_name):
        return "hello"

    monkeypatch.setattr(bot.media, "video_frames_and_audio", fake_frames)
    monkeypatch.setattr(bot.media, "transcribe", fake_transcribe)
    message = SimpleNamespace(
        author=SimpleNamespace(id=2, name="luka", display_name="Luka")
    )
    images: list[dict[str, str]] = []
    notes: list[str] = []
    editable: list[context.EditableImage] = []
    await bot.read_attachment(
        message, attachment("clip.mp4", "video/mp4"), images, notes, editable
    )
    assert editable == []  # video frames go to the prompt, but are not editable
    assert len(images) == 1
