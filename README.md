# Debeloglav

A Discord bot that works like @grok on X: tag it in a message and it answers in a thread on that message. It searches the web to research and fact-check before answering, and lists its sources under the reply. It understands images, voice messages, videos and documents (PDF, Word, PowerPoint, text and code files), and can answer with a generated image.

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
- **Threads:** tagged in a channel, it opens a thread on your message (named after the question) and answers there, so conversations don't clutter the channel. In its own threads it answers every message, no tag needed; in threads someone else opened it answers only tags and replies to its messages. Inside a thread, in a DM, or where it may not open a thread, it replies inline. Threads are archived after 24 idle hours. Both can be changed, or threads switched off, in the dashboard.
- In DMs it answers every message.
- It always talks in casual Slovenian (tika, no formal phrases) and picks up the server's slang from recent messages and from what it remembers. Teach it words with `@Debeloglav pri nas 'X' pomeni Y`.
- It never pings `@everyone`, `@here`, roles or other users.
- While it works, it reacts to your message with 🤔, and removes the reaction when it replies.

### Conversations and memory

- Reply to any of Debeloglav's messages (no tag needed) to continue that conversation: it remembers what was said. Each conversation is an Agents SDK session, journaled in `data/sessions/`, so it survives a restart and is kept for 6 idle hours.
- `@Debeloglav zapomni si, da je Tim admin` → saved for the whole server and known in every conversation. `pozabi #3` deletes it.

### Learning how the server talks

- Once a day it reads the server's recent messages (the last week on the first run, then everything since the last run) and updates a short **style guide**: tone, words and slang, nicknames, inside jokes, how much emoji, swearing and English, and typical phrases. Every reply uses it.
- It describes how people talk, not what they said, and leaves out personal data. It reads every text channel the bot can read, private ones included, except the channels blocked in the dashboard. Bot messages are skipped.
- A run with fewer than 20 messages is skipped and doesn't count as learned, so the next run (3 hours later) still reads everything since the last guide. The dashboard's Learning page lists each run with the channels it left out and why.
- `@Debeloglav kaj si se naučil o nas?` shows the guide.
- Learning is turned on or off, or relearned, in the [admin dashboard](#admin-dashboard).

The guide is stored in SQLite next to memories and tasks. Code: `debeloglav/style.py`.

### Searching the server's history

- `@Debeloglav kdaj je @Luka rekel, da je pizza overrated?`, `kdo je poslal tisti link do Steama?`, `poišči slike iz #izlet od marca` → it searches the server's messages with Discord's search, and answers with quotes, who, when and links to the messages.
- It shows messages from every channel the person asking can read, except the channels blocked in the dashboard and age-restricted ones.
- It reads 25 messages per search by default and up to 200 when it asks for more, and it always gets the total number of matches, so it can answer `koliko sporočil je napisal @Luka?`.

### Catching up

- `@Debeloglav kaj sem zamudil?` or `kaj sem zamudil v #general?` → it reads the channel since your last message there (up to 300 messages) and summarizes what you missed: the main topics, decisions, plans and anything addressed to you, with links to the key messages. If you haven't written there recently, it covers the last 300 messages. Code: `debeloglav/catchup.py`.
- `/zamujeno [kanal]` → the same summary, but privately: a slash command for everyone that answers ephemerally (only you see it), in this channel or in the one you name. Code: `debeloglav/commands.py`.

### Background research

- `@Debeloglav razišči podrobno …` → it answers that it's on it, reacts with 🔬, and a research lead agent splits the question and runs up to 5 researcher subagents in parallel. When the report is ready it's posted as a reply to your message and the 🔬 is removed. Reply to the report to ask follow-ups.

### Scheduled tasks

- Reminders: `@Debeloglav spomni me čez 2 uri, da pokličem zobozdravnika` (pings you).
- Recurring jobs: `@Debeloglav vsak delavnik ob 8h objavi povzetek AI novic`.
- Watches: `@Debeloglav vsake 3 ure preveri, ali je izšel GTA 6 trailer, in mi povej` → it posts only when there's something new, and cancels itself when done.
- `@Debeloglav katere naloge imamo?` / `prekliči nalogo #2`.

Tasks are stored in SQLite (`data/debeloglav.db`) and survive restarts. Recurring tasks run at most every 15 minutes. Each server can have 25 tasks, each person 10. Only the person who created a task, or a server admin, can cancel it; the same goes for deleting memories.

### Admin dashboard

The bot also serves a small password-protected **admin dashboard** (server-rendered HTML, no JavaScript; the Geist fonts are bundled in `debeloglav/static/` under the SIL Open Font License, so it loads nothing from other sites). Set `DASHBOARD_PASSWORD` in `.env` to enable it; it listens on `DASHBOARD_HOST` (default `127.0.0.1`) and `DASHBOARD_PORT` (default `8080`) and shows a login page at `/login` — enter just that password (no username) and you get a `HttpOnly` session cookie that lasts a week (or until you log out; a restart logs everyone out). With Docker Compose it is published on the host's loopback only, so open <http://localhost:8080>. Over SSH, forward it first: `ssh -L 8080:localhost:8080 host`, then open <http://localhost:8080> locally.

Failed logins are rate-limited: after 5 wrong passwords from one client IP within 15 minutes that IP is locked out for 15 minutes (HTTP 429 with `Retry-After` — even the correct password is refused during the lockout), and more than 50 failures from all IPs together trigger a global 15-minute lockout that only the correct password gets through. Behind a reverse proxy every request arrives from the proxy's own address, so you must set `DASHBOARD_TRUSTED_PROXIES` to the proxy's IP as the dashboard sees it (with Docker Compose that is the Docker bridge gateway, e.g. `172.17.0.1`); without it the `X-Forwarded-For` header is ignored, because anyone can spoof it.

It shows what the bot is doing — recent requests with their full prompts and replies, the prompts it actually gets, style-learning runs and guides, memories, blocked channels and active tasks, and the last errors — and it changes the settings, memories, blocked channels, style learning and task cancellation.

Every request is traced: how long each stage took (reading the context, the agent's turn, sending the reply), and every step of the turn with its time — each tool call and result, when the model started writing — plus the model and reasoning effort it ran with. A request's page shows this as a timeline, so a slow reply shows whether the time went to the model thinking, a tool, or Discord. The overview shows the median and p95 reply time, the average split per stage and the tools' timings. Export buttons download one request as JSON, the request list as JSON or CSV, the learning history, or everything at once (`/export/all.json`).

The Usage page shows the tokens the models used and roughly what they cost: today, the last 7 and 30 days and all time, per model and per day. A request's page shows its own tokens and cost. The cost is the tokens times the *Model prices* setting (US dollars per million input, output and cached input tokens, one model per line), computed when the page is shown, so a corrected price also re-prices the past. It is an estimate: check the prices against [Mistral's pricing](https://docs.mistral.ai/inference/pricing), and note that transcription, OCR and the per-call fees of web search and image generation are not billed by the token and are not included. Usage is counted from the release that added the page; earlier requests have none.

Every setting of the bot is changed on the Settings page, and only there: the models and reasoning effort, extra instructions, context messages, video frames, the thinking emote, the time zone, the conversation timeout, threads (on/off and when they archive), the research, memory and task limits, and how often and from how many messages the style is learned. They are not read from `.env`. A changed value is stored in SQLite and takes effect with the next message, no restart; *Reset to default* goes back to the built-in default. It is the only place to change server settings; the bot has no admin slash commands. Blocked channels (a channel, or a whole category) are stored in SQLite, per server, and the bot stops reading them everywhere: messages, history search, catch-up and style learning. Code: `debeloglav/dashboard.py`.

## How it works

- `debeloglav/agent.py`: the agent. `agents.Agent(model="mistral-large-4", tools=[web_search, ...])`. The `web_search` tool calls Mistral's hosted `web_search` through the Conversations API (the pattern the Agents SDK docs recommend) and returns a summary with source URLs. The agent answers in plain text; the sources its searches found are recorded by the tool and listed under the reply.
- `debeloglav/agent.py` also has `generate_image`, which calls the hosted `image_generation` tool, downloads the image from the URL the tool returns, and records the bytes. The bot attaches them to the reply. `edit_image` edits one of the turn's images the same way: it uploads the image, passes its signed URL to the tool, and accepts the result only if the tool really used that URL; otherwise it describes the image with the vision model, regenerates it with the change, and the reply notes it is a new version, not an edit.
- `debeloglav/agent.py` also has the memory, scheduling and research tools, and the research agent (`build_research_agent`) with its `researcher` subagent.
- `debeloglav/media.py`: Voxtral transcription, and ffmpeg frame and audio extraction for videos.
- `debeloglav/scheduler.py`: the loop that runs due tasks. `debeloglav/store.py`: SQLite for memories and tasks.
- `debeloglav/settings.py`: every setting, with its default and limits; the values changed in the dashboard live in SQLite.
- `debeloglav/trace.py`: times each request's stages and records the steps of its turn from the Agents SDK's events, for the dashboard.
- `debeloglav/usage.py`: counts the tokens of every model call (the agents' through a hook, the bot's own API calls from their responses) into per-day totals in SQLite, and prices them for the dashboard.
- `debeloglav/context.py`: which Discord server, channel and person each running session acts for. Tools read it through their `ToolCallbackContext`; the model never sees it.
- `debeloglav/bot.py`: the Discord side. Decides when it's summoned, gathers context (reply chain, recent messages, attachments), runs the agent, and replies.

Add more tools by writing a function and putting it in `tools=[...]`, or give the agent connectors, MCP servers, skills or subagents. See the [Agents SDK docs](https://docs.mistral.ai/studio/agents/learn/agent/agent).

## Setup

1. Create an application at <https://discord.com/developers/applications> and name it **Debeloglav**.
2. **Bot** tab: reset and copy the token, and enable **Message Content Intent**.
3. **OAuth2 → URL Generator**: scopes `bot applications.commands`, permissions `View Channels`, `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History`, `Add Reactions`, `Attach Files`. Open the URL to invite the bot. (`applications.commands` is needed for the `/zamujeno` slash command.)
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

`.env` holds only the secrets and what is needed before the database opens. Everything else is a setting in the [admin dashboard](#admin-dashboard).

| Variable           | Default           | Description                                             |
| ------------------ | ----------------- | ------------------------------------------------------- |
| `DISCORD_TOKEN`    | –                 | Discord bot token                                       |
| `MISTRAL_API_KEY`  | –                 | Mistral API key                                         |
| `DASHBOARD_PASSWORD` | –               | Password for the admin web dashboard the bot serves (empty = disabled). See [Admin dashboard](#admin-dashboard) |
| `DB_PATH`          | `data/debeloglav.db` | SQLite file for memories, tasks and settings         |
| `DASHBOARD_HOST`   | `127.0.0.1`       | Address the admin dashboard binds to (Compose sets `0.0.0.0` and publishes `127.0.0.1:8080:8080`) |
| `DASHBOARD_PORT`   | `8080`            | Port the admin dashboard listens on |
| `DASHBOARD_TRUSTED_PROXIES` | – (trust none) | Comma-separated IPs of the reverse proxy in front of the dashboard (with Docker Compose: the Docker bridge gateway, e.g. `172.17.0.1`, as the container sees it). Only from these is `X-Forwarded-For` trusted, so the failed-login lockout sees the real client IP; empty = the header is ignored as spoofable |

Conversations are saved in `data/` (the message-to-session map in SQLite, the sessions as journals in `data/sessions/`), so a restart continues them. Running research still lives in the bot's memory, so a restart ends it. Memories and scheduled tasks are kept too.

Don't set an `AGENT` environment variable: the Mistral workflows config reads it and fails to start if it isn't its own config.
