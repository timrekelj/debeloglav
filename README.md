# Boštjan

A Discord bot that works like @grok on X: tag it in a message and it replies to that message. It searches the web to research and fact-check before answering, and lists its sources under the reply.

Runs on **Mistral Large 4** (`mistral-large-4`) through Mistral's Conversations API with the built-in `web_search` tool.

## How it behaves

- `@Boštjan who won the last Tour de France?` → it searches and answers.
- Reply to someone's message with `@Boštjan is this true?` (or just `@Boštjan`) → it fact-checks the message you replied to.
- It reads the reply thread and the last few channel messages for context.
- In DMs it answers every message.
- It never pings `@everyone`, `@here`, roles or other users.

## Setup

1. Create an application at <https://discord.com/developers/applications>, name it **Boštjan**.
2. **Bot** tab: reset and copy the token, and enable **Message Content Intent**.
3. **OAuth2 → URL Generator**: scope `bot`, permissions `View Channels`, `Send Messages`, `Send Messages in Threads`, `Read Message History`. Open the URL to invite the bot.
4. Get an API key at <https://console.mistral.ai>.
5. Configure and run:

```sh
cp .env.example .env   # fill in DISCORD_TOKEN and MISTRAL_API_KEY
bun install
bun start              # or: bun dev (restarts on change)
```

## Configuration

| Variable           | Default           | Description                                        |
| ------------------ | ----------------- | -------------------------------------------------- |
| `DISCORD_TOKEN`    | –                 | Discord bot token                                  |
| `MISTRAL_API_KEY`  | –                 | Mistral API key                                    |
| `MISTRAL_MODEL`    | `mistral-large-4` | Any Mistral model that supports built-in tools     |
| `CONTEXT_MESSAGES` | `10`              | How many earlier channel messages to include (0 = none) |
