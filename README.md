# Boštjan

A Discord bot that works like @grok on X: tag it in a message and it replies to that message. It searches the web to research and fact-check before answering, and lists its sources under the reply. It understands images, voice messages and videos, and can answer with a generated image.

Built with [`mistralai-agents`](https://docs.mistral.ai/studio/agents/getting-started/quickstart) (Mistral's Agents SDK) on **Mistral Large 4** (`mistral-large-4`), and [discord.py](https://discordpy.readthedocs.io).

## How it behaves

- `@Boštjan who won the last Tour de France?` → it searches and answers.
- Reply to someone's message with `@Boštjan is this true?` (or just `@Boštjan`) → it fact-checks the message you replied to.
- Send or reply to an **image** and ask about it: Mistral Large 4 sees images.
- Send or reply to a **voice message** (or audio file): it's transcribed with Voxtral and answered.
- Send or reply to a **video**: it samples frames with ffmpeg and transcribes the sound (Mistral Large 4 has no native video input), then answers about both.
- `@Boštjan nariši mačko v ljubljanski kavarni` → it **generates an image** (Mistral's `image_generation` tool) and attaches it to the reply.
- It reads the reply thread and the last few channel messages for context.
- In DMs it answers every message.
- It never pings `@everyone`, `@here`, roles or other users.

## How it works

- `bostjan/agent.py`: the agent. `agents.Agent(model="mistral-large-4", tools=[web_search], output_type=Reply)`. The `web_search` tool calls Mistral's hosted `web_search` through the Conversations API (the pattern the Agents SDK docs recommend) and returns a summary with source URLs. The agent answers with a `Reply` (text + the sources it used).
- `bostjan/agent.py` also has `generate_image`, which calls the hosted `image_generation` tool and returns file ids. The agent puts them in `Reply.images` and the bot downloads and attaches them.
- `bostjan/media.py`: Voxtral transcription, and ffmpeg frame and audio extraction for videos.
- `bostjan/bot.py`: the Discord side. Decides when it's summoned, gathers context (reply chain, recent messages, attachments), runs the agent, and replies.

Add more tools by writing a function and putting it in `tools=[...]`, or give the agent connectors, MCP servers, skills or subagents. See the [Agents SDK docs](https://docs.mistral.ai/studio/agents/learn/agent/agent).

## Setup

1. Create an application at <https://discord.com/developers/applications> and name it **Boštjan**.
2. **Bot** tab: reset and copy the token, and enable **Message Content Intent**.
3. **OAuth2 → URL Generator**: scope `bot`, permissions `View Channels`, `Send Messages`, `Send Messages in Threads`, `Read Message History`. Open the URL to invite the bot.
4. Get an API key at <https://console.mistral.ai>.
5. Configure and run (needs [uv](https://docs.astral.sh/uv/), and [ffmpeg](https://ffmpeg.org) for videos):

```sh
cp .env.example .env   # fill in DISCORD_TOKEN and MISTRAL_API_KEY
uv run --env-file .env python -m bostjan
```

## Configuration

| Variable           | Default           | Description                                             |
| ------------------ | ----------------- | ------------------------------------------------------- |
| `DISCORD_TOKEN`    | –                 | Discord bot token                                       |
| `MISTRAL_API_KEY`  | –                 | Mistral API key                                         |
| `MISTRAL_MODEL`    | `mistral-large-4` | Model for the agent and for web search                  |
| `CONTEXT_MESSAGES` | `10`              | How many earlier channel messages to include (0 = none) |
| `TRANSCRIBE_MODEL` | `voxtral-mini-transcribe-3` | Model for voice messages and video sound   |
| `VIDEO_FRAMES`     | `6`               | How many frames to sample from a video                  |

Don't set an `AGENT` environment variable: the Mistral workflows config reads it and fails to start if it isn't its own config.
