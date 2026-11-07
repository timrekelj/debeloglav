# Debeloglav

A Discord bot that works like @grok on X: tag it in a message and it replies to that message. It searches the web to research and fact-check before answering, and lists its sources under the reply. It understands images, voice messages, videos and documents (PDF, Word, PowerPoint, text and code files), and can answer with a generated image.

Built with [`mistralai-agents`](https://docs.mistral.ai/studio/agents/getting-started/quickstart) (Mistral's Agents SDK) on **Mistral Large 4** (`mistral-large-4`), and [discord.py](https://discordpy.readthedocs.io).

## How it behaves

- `@Debeloglav who won the last Tour de France?` → it searches and answers.
- Reply to someone's message with `@Debeloglav is this true?` (or just `@Debeloglav`) → it fact-checks the message you replied to.
- Send or reply to an **image** and ask about it: Mistral Large 4 sees images.
- Reply to an image (yours, or one Debeloglav generated) with `@Debeloglav dodaj mu klobuk` → it **edits the image** (the hosted `image_generation` tool with the image as input) and attaches the edited version. If a real edit isn't possible, it generates a new version instead and notes that under the reply.
- Send or reply to a **voice message** (or audio file): it's transcribed with Voxtral and answered.
- Send or reply to a **video**: it samples frames with ffmpeg and transcribes the sound (Mistral Large 4 has no native video input), then answers about both.
- It reads **polls** wherever it reads messages: the question, each answer with its vote count, and whether the poll ended.
- `@Debeloglav nariši mačko v ljubljanski kavarni` → it **generates an image** (Mistral's `image_generation` tool) and attaches it to the reply.
- It reads the reply thread and the last few channel messages for context.
- In DMs it answers every message.
- It always talks in casual Slovenian (tika, no formal phrases) and picks up the server's slang from recent messages and from what it remembers. Teach it words with `@Debeloglav pri nas 'X' pomeni Y`.
- It never pings `@everyone`, `@here`, roles or other users.
- While it works, it reacts to your message with 🤔, and removes the reaction when it replies.

### Conversations and memory

- Reply to any of Debeloglav's messages (no tag needed) to continue that conversation: it remembers what was said. Each conversation is an Agents SDK session, journaled in `data/sessions/`, so it survives a restart and is kept for 6 idle hours.
- `@Debeloglav zapomni si, da je Tim admin` → saved for the whole server and known in every conversation. `pozabi #3` deletes it.

### Learning how the server talks

- Once a day it reads the server's recent messages (the last week on the first run, then everything since the last run) and updates a short **style guide**: tone, words and slang, nicknames, inside jokes, how much emoji, swearing and English, and typical phrases. Every reply uses it.
- It describes how people talk, not what they said, and leaves out personal data. By default it reads only channels everyone in the server can read, because the guide is used in every channel. Bot messages are skipped. Admins can name other channels explicitly.
- `@Debeloglav kaj si se naučil o nas?` shows the guide.
- Admins (Manage Server permission) can change it: `uči se samo iz #general in #memes`, `uči se iz vseh kanalov`, `nauči se znova`, or `nehaj se učiti` (which also deletes the guide).

The guide is stored in SQLite next to memories and tasks. Code: `debeloglav/style.py`.

### Searching the server's history

- `@Debeloglav kdaj je @Luka rekel, da je pizza overrated?`, `kdo je poslal tisti link do Steama?`, `poišči slike iz #izlet od marca` → it searches the server's messages with Discord's search, and answers with quotes, who, when and links to the messages.
- It only shows messages from channels the person asking can read, and skips age-restricted channels.
- The answer is posted in the channel where the question was asked, so it also leaves out messages from channels not everyone in the server can read (counted in `hidden_private`); for those, `/zamujeno` gives a private summary.

### Catching up

- `@Debeloglav kaj sem zamudil?` or `kaj sem zamudil v #general?` → it reads the channel since your last message there (up to 300 messages) and summarizes what you missed: the main topics, decisions, plans and anything addressed to you, with links to the key messages. If you haven't written there recently, it covers the last 300 messages. Code: `debeloglav/catchup.py`.
- A channel not everyone in the server can read is never summarized in a public answer: the bot refuses and points to `/zamujeno`, which only the asker sees.
- `/zamujeno [kanal]` → the same summary, but privately: a slash command for everyone that answers ephemerally (only you see it), in this channel or in the one you name. Code: `debeloglav/commands.py`.

### Background research

- `@Debeloglav razišči podrobno …` → it answers that it's on it, reacts with 🔬, and a research lead agent splits the question and runs up to 5 researcher subagents in parallel. When the report is ready it's posted as a reply to your message and the 🔬 is removed. Reply to the report to ask follow-ups.

### Scheduled tasks

- Reminders: `@Debeloglav spomni me čez 2 uri, da pokličem zobozdravnika` (pings you).
- Recurring jobs: `@Debeloglav vsak delavnik ob 8h objavi povzetek AI novic`.
- Watches: `@Debeloglav vsake 3 ure preveri, ali je izšel GTA 6 trailer, in mi povej` → it posts only when there's something new, and cancels itself when done.
- `@Debeloglav katere naloge imamo?` / `prekliči nalogo #2`.

Tasks are stored in SQLite (`data/debeloglav.db`) and survive restarts. Recurring tasks run at most every 15 minutes. Each server can have 25 tasks, each person 10. Only the person who created a task, or a server admin, can cancel it; the same goes for deleting memories.

### Admin slash commands

Server admins (Manage Server permission) also have the `/debeloglav` command group. Every reply is ephemeral, visible only to the admin who ran the command:

- `/debeloglav nastavitve` → blocked channels (from `.env` and this server), style-learning state, number of memories and active tasks.
- `/debeloglav blokiraj kanal` / `/debeloglav odblokiraj kanal` → block or unblock a channel, category, forum or thread on this server only. The bot then stops reading it everywhere: messages, history search and style learning. A channel blocked in `.env` can only be unblocked there.
- `/debeloglav ucenje akcija` → `vklop` / `izklop` (also deletes the learned guide) / `znova` (learn it again) / `vsi_kanali`, with optional `kanali` to limit learning to the named channels.
- `/debeloglav spomini` / `/debeloglav pozabi id` → list, or delete, this server's memories.
- `/debeloglav naloge` / `/debeloglav preklici id` → list, or cancel, this server's active tasks.

Everyone, not just admins, also has `/zamujeno [kanal]`: an ephemeral (private) summary of what they missed in a channel — see [Catching up](#catching-up).

Per-server blocked channels are stored in SQLite (the `blocked_channels` table), so they survive restarts. Code: `debeloglav/commands.py`.

## How it works

- `debeloglav/agent.py`: the agent. `agents.Agent(model="mistral-large-4", tools=[web_search, ...])`. The `web_search` tool calls Mistral's hosted `web_search` through the Conversations API (the pattern the Agents SDK docs recommend) and returns a summary with source URLs. The agent answers in plain text; the sources its searches found are recorded by the tool and listed under the reply.
- `debeloglav/agent.py` also has `generate_image`, which calls the hosted `image_generation` tool, downloads the image from the URL the tool returns, and records the bytes. The bot attaches them to the reply. `edit_image` edits one of the turn's images the same way: it uploads the image, passes its signed URL to the tool, and accepts the result only if the tool really used that URL; otherwise it describes the image with the vision model, regenerates it with the change, and the reply notes it is a new version, not an edit.
- `debeloglav/agent.py` also has the memory, scheduling and research tools, and the research agent (`build_research_agent`) with its `researcher` subagent.
- `debeloglav/media.py`: Voxtral transcription, and ffmpeg frame and audio extraction for videos.
- `debeloglav/scheduler.py`: the loop that runs due tasks. `debeloglav/store.py`: SQLite for memories and tasks.
- `debeloglav/context.py`: which Discord server, channel and person each running session acts for. Tools read it through their `ToolCallbackContext`; the model never sees it.
- `debeloglav/bot.py`: the Discord side. Decides when it's summoned, gathers context (reply chain, recent messages, attachments), runs the agent, and replies.

Add more tools by writing a function and putting it in `tools=[...]`, or give the agent connectors, MCP servers, skills or subagents. See the [Agents SDK docs](https://docs.mistral.ai/studio/agents/learn/agent/agent).

## Setup

1. Create an application at <https://discord.com/developers/applications> and name it **Debeloglav**.
2. **Bot** tab: reset and copy the token, and enable **Message Content Intent**.
3. **OAuth2 → URL Generator**: scopes `bot applications.commands`, permissions `View Channels`, `Send Messages`, `Send Messages in Threads`, `Read Message History`, `Add Reactions`, `Attach Files`. Open the URL to invite the bot. (`applications.commands` is needed for the slash commands, `/zamujeno` and `/debeloglav`.)
4. Get an API key at <https://console.mistral.ai>.
5. Configure and run (needs [uv](https://docs.astral.sh/uv/), and [ffmpeg](https://ffmpeg.org) for videos):

```sh
cp .env.example .env   # fill in DISCORD_TOKEN and MISTRAL_API_KEY
uv run --env-file .env python -m debeloglav
```

### On a server, with Docker

The image has Python, every library and ffmpeg; you only need Docker with Compose.

```sh
git clone https://github.com/timrekelj/debeloglav.git && cd debeloglav
cp .env.example .env              # fill in DISCORD_TOKEN and MISTRAL_API_KEY
mkdir -p data && sudo chown 1000:1000 data   # the bot runs as user 1000 and keeps its database here
docker compose up -d --build
docker compose logs -f            # watch it log in
```

Memories, tasks, the style guide and the conversations' sessions live in `data/`, so they survive restarts and rebuilds; to move them, copy `data/debeloglav.db` and `data/sessions/` from the old machine before starting. Update with `git pull && docker compose up -d --build`. Run only one copy of the bot per token, or every message gets answered twice.

## Configuration

| Variable           | Default           | Description                                             |
| ------------------ | ----------------- | ------------------------------------------------------- |
| `DISCORD_TOKEN`    | –                 | Discord bot token                                       |
| `MISTRAL_API_KEY`  | –                 | Mistral API key                                         |
| `MISTRAL_MODEL`    | `mistral-large-4` | Model that writes the answers                           |
| `TOOL_MODEL`       | `mistral-medium-latest` | Model for the hosted web search and image generation (about 3x faster than Large 4) |
| `REASONING_EFFORT` | `low`             | How much the model thinks before normal replies: low, medium or high |
| `CONTEXT_MESSAGES` | `10`              | How many earlier channel messages to include (0 = none) |
| `TRANSCRIBE_MODEL` | `voxtral-mini-transcribe-3` | Model for voice messages and video sound   |
| `VIDEO_FRAMES`     | `6`               | How many frames to sample from a video                  |
| `OCR_MODEL`        | `mistral-ocr-latest` | Model that reads PDF, Word and PowerPoint files      |
| `TIMEZONE`         | `Europe/Ljubljana` | Time zone for reminders and recurring tasks            |
| `DB_PATH`          | `data/debeloglav.db` | SQLite file for memories and tasks                      |
| `MAX_RESEARCH_JOBS` | `3`              | How many research jobs can run at once                  |
| `BLOCKED_CHANNELS` | –                 | Comma-separated channel or category ids the bot never reads (a global list): it ignores messages there, and leaves them out of search and style learning. Admins can also block channels per server with `/debeloglav blokiraj` (stored in the database) |

Conversations are saved in `data/` (the message-to-session map in SQLite, the sessions as journals in `data/sessions/`), so a restart continues them. Running research still lives in the bot's memory, so a restart ends it. Memories and scheduled tasks are kept too.

Don't set an `AGENT` environment variable: the Mistral workflows config reads it and fails to start if it isn't its own config.
