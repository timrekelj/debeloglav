"""The run_python tool, with the Mistral client mocked: output, image files, the cap, failures."""

import pytest
from mistralai import agents
from mistralai.client import models as api_models
from test_images import SESSION, env, message_entry, response, tool_entry  # noqa: F401

from debeloglav import agent, context

PNG = b"\x89PNG\r\n\x1a\n" + b"data"
JPEG = b"\xff\xd8\xff" + b"data"


def code_entry(output="sum 6\n", code="print(6)"):
    entry = tool_entry({"code": code}, name="code_interpreter")
    entry.info = {
        "result": [{"type": "text", "text": output}],
        "code": code,
        "code_output": output,
    }
    return entry


def chunk(file_id, name="plot_0.png", file_type="png"):
    return api_models.ToolFileChunk(
        tool="code_interpreter", file_id=file_id, file_name=name, file_type=file_type
    )


def found_images():
    return [img.data for img in context.take_found(SESSION).images]


async def test_run_python_returns_the_output_and_records_the_png(env):  # noqa: F811
    env.files.downloaded["f1"] = PNG
    env.conversations.responses.append(
        response(code_entry("sum 6\n"), message_entry([chunk("f1")]))
    )
    result = await agent.run_python.__wrapped__(env.ctx, "print(6)")
    assert result == {"output": "sum 6", "images_attached": 1}
    assert found_images() == [PNG]
    call = env.conversations.calls[0]
    assert call["tools"] == [{"type": "code_interpreter"}]
    assert "print(6)" in call["inputs"]
    assert call["store"] is False


async def test_run_python_ignores_files_that_are_not_images(env):  # noqa: F811
    env.files.downloaded.update({"f1": b"a,b\n1,2\n", "f2": JPEG, "f3": b"%PDF-1.7"})
    env.conversations.responses.append(
        response(
            code_entry(),
            message_entry(
                [
                    chunk("f1", "data.csv", "csv"),
                    chunk("f2", "photo.jpg", "jpg"),
                    chunk("f3", "doc.pdf", "pdf"),
                ]
            ),
        )
    )
    result = await agent.run_python.__wrapped__(env.ctx, "x")
    assert result["images_attached"] == 1
    assert found_images() == [JPEG]


async def test_run_python_attaches_at_most_four_images(env):  # noqa: F811
    ids = [f"f{i}" for i in range(6)]
    for file_id in ids:
        env.files.downloaded[file_id] = PNG + file_id.encode()
    env.conversations.responses.append(
        response(code_entry(), message_entry([chunk(i) for i in ids]))
    )
    result = await agent.run_python.__wrapped__(env.ctx, "x")
    assert result["images_attached"] == 4
    assert found_images() == [PNG + i.encode() for i in ids[:4]]


async def test_run_python_cuts_long_output(env):  # noqa: F811
    env.conversations.responses.append(response(code_entry("x" * 10000)))
    result = await agent.run_python.__wrapped__(env.ctx, "x")
    assert len(result["output"]) < 4100
    assert result["output"].endswith("[output cut off]")
    assert result["images_attached"] == 0


async def test_run_python_fails_when_the_code_did_not_run(env):  # noqa: F811
    env.conversations.responses.append(response(message_entry("I cannot run code.")))
    with pytest.raises(agents.ToolError):
        await agent.run_python.__wrapped__(env.ctx, "print(1)")
    assert context.take_found(SESSION).images == []


def test_run_python_is_a_tool_of_the_main_agent_only():
    def names(built):
        return {tool.name for tool in built._tool_objects()}

    assert "run_python" in names(agent.build_agent())
    assert "run_python" not in names(agent.build_research_agent())
