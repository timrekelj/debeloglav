# Debeloglav

A Discord bot that works like @grok on X: tag it in a message and it replies to that message. It searches the web to research and fact-check before answering, and lists its sources under the reply. It understands images, voice messages and videos, and can answer with a generated image.

Built with [`mistralai-agents`](https://docs.mistral.ai/studio/agents/getting-started/quickstart) (Mistral's Agents SDK) on **Mistral Large 4** (`mistral-large-4`), and [discord.py](https://discordpy.readthedocs.io).

## How it behaves

- `@Debeloglav who won the last Tour de France?` → it searches and answers.
- Reply to someone's message with `@Debeloglav is this true?` (or just `@Debeloglav`) → it fact-checks the message you replied to.
- Send or reply to an **image** and ask about it: Mistral Large 4 sees images.
- Send or reply to a **voice message** (or audio file): it's transcribed with Voxtral and answered.
- Send or reply to a **video**: it samples frames with ffmpeg and transcribes the sound (Mistral Large 4 has no native video input), then answers about both.
- `@Debeloglav nariši mačko v ljubljanski kavarni` → it **generates an image** (Mistral's `image_generation` tool) and attaches it to the reply.
- It reads the reply thread and the last few channel messages for context.
- In DMs it answers every message.
- It always talks in casual Slovenian (tika, no formal phrases) and picks up the server's slang from recent messages and from what it remembers. Teach it words with `@Debeloglav pri nas 'X' pomeni Y`.
- It never pings `@everyone`, `@here`, roles or other users.
- While it works, it reacts to your message with 🤔, and removes the reaction when it replies.

### Conversations and memory

- Reply to any of Debeloglav's messages (no tag needed) to continue that conversation: it remembers what was said. Each conversation is an Agents SDK session, kept for 6 idle hours.
- `@Debeloglav zapomni si, da je Tim admin` → saved for the whole server and known in every conversation. `pozabi #3` deletes it.

### Learning how the server talks

- Once a day it reads the server's recent messages (the last week on the first run, then everything since the last run) and updates a short **style guide**: tone, words and slang, nicknames, inside jokes, how much emoji, swearing and English, and typical phrases. Every reply uses it.
- It describes how people talk, not what they said, and leaves out personal data. Bot messages and channels it can't read are skipped.
- `@Debeloglav kaj si se naučil o nas?` shows the guide.
- Admins (Manage Server permission) can change it: `uči se samo iz #general in #memes`, `uči se iz vseh kanalov`, `nauči se znova`, or `nehaj se učiti` (which also deletes the guide).

The guide is stored in SQLite next to memories and tasks. Code: `debeloglav/style.py`.

### Background research

- `@Debeloglav razišči podrobno …` → it answers that it's on it, reacts with 🔬, and a research lead agent splits the question and runs up to 5 researcher subagents in parallel. When the report is ready it's posted as a reply to your message and the 🔬 is removed. Reply to the report to ask follow-ups.

### Scheduled tasks

- Reminders: `@Debeloglav spomni me čez 2 uri, da pokličem zobozdravnika` (pings you).
- Recurring jobs: `@Debeloglav vsak delavnik ob 8h objavi povzetek AI novic`.
- Watches: `@Debeloglav vsake 3 ure preveri, ali je izšel GTA 6 trailer, in mi povej` → it posts only when there's something new, and cancels itself when done.
- `@Debeloglav katere naloge imamo?` / `prekliči nalogo #2`.

Tasks are stored in SQLite (`data/debeloglav.db`) and survive restarts. Recurring tasks run at most every 15 minutes.

## How it works

- `debeloglav/agent.py`: the agent. `agents.Agent(model="mistral-large-4", tools=[web_search], output_type=Reply)`. The `web_search` tool calls Mistral's hosted `web_search` through the Conversations API (the pattern the Agents SDK docs recommend) and returns a summary with source URLs. The agent answers with a `Reply` (text + the sources it used).
- `debeloglav/agent.py` also has `generate_image`, which calls the hosted `image_generation` tool and returns file ids. The agent puts them in `Reply.images` and the bot downloads and attaches them.
- `debeloglav/agent.py` also has the memory, scheduling and research tools, and the research agent (`build_research_agent`) with its `researcher` subagent.
- `debeloglav/media.py`: Voxtral transcription, and ffmpeg frame and audio extraction for videos.
- `debeloglav/scheduler.py`: the loop that runs due tasks. `debeloglav/store.py`: SQLite for memories and tasks.
- `debeloglav/context.py`: which Discord server, channel and person each running session acts for. Tools read it through their `ToolCallbackContext`; the model never sees it.
- `debeloglav/bot.py`: the Discord side. Decides when it's summoned, gathers context (reply chain, recent messages, attachments), runs the agent, and replies.

Add more tools by writing a function and putting it in `tools=[...]`, or give the agent connectors, MCP servers, skills or subagents. See the [Agents SDK docs](https://docs.mistral.ai/studio/agents/learn/agent/agent).

## Setup

1. Create an application at <https://discord.com/developers/applications> and name it **Debeloglav**.
2. **Bot** tab: reset and copy the token, and enable **Message Content Intent**.
3. **OAuth2 → URL Generator**: scope `bot`, permissions `View Channels`, `Send Messages`, `Send Messages in Threads`, `Read Message History`, `Add Reactions`, `Attach Files`. Open the URL to invite the bot.
4. Get an API key at <https://console.mistral.ai>.
5. Configure and run (needs [uv](https://docs.astral.sh/uv/), and [ffmpeg](https://ffmpeg.org) for videos):

```sh
cp .env.example .env   # fill in DISCORD_TOKEN and MISTRAL_API_KEY
uv run --env-file .env python -m debeloglav
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
| `TIMEZONE`         | `Europe/Ljubljana` | Time zone for reminders and recurring tasks            |
| `DB_PATH`          | `data/debeloglav.db` | SQLite file for memories and tasks                      |
| `MAX_RESEARCH_JOBS` | `3`              | How many research jobs can run at once                  |

Conversations and running research live in the bot's memory, so a restart ends them. Memories and scheduled tasks are kept.

Don't set an `AGENT` environment variable: the Mistral workflows config reads it and fails to start if it isn't its own config.
