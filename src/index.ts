import {
  Client,
  Events,
  GatewayIntentBits,
  Partials,
  type Message,
} from "discord.js";
import { ask, type Answer } from "./mistral";

const DISCORD_TOKEN = requireEnv("DISCORD_TOKEN");
const MISTRAL_API_KEY = requireEnv("MISTRAL_API_KEY");
const MISTRAL_MODEL = process.env.MISTRAL_MODEL ?? "mistral-large-4";
const CONTEXT_MESSAGES = Number(process.env.CONTEXT_MESSAGES ?? 10);
const REPLY_CHAIN_DEPTH = 8;
const MAX_SOURCES = 5;
const DISCORD_LIMIT = 2000;

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    console.error(`Missing ${name} in environment (see .env.example)`);
    process.exit(1);
  }
  return value;
}

function instructions(botName: string): string {
  const today = new Date().toISOString().slice(0, 10);
  return `You are ${botName} (Boštjan), a witty, sharp and helpful assistant living in a Discord server, similar to Grok on X.
People summon you by tagging you in a message. Today's date is ${today}.

How to behave:
- Answer the question in the tagged message. Use the surrounding conversation only as context.
- If the tagged message is a reply with little or no question (for example "is this true?" or just a tag), fact-check or explain the message it replies to.
- Use the web_search tool whenever the question involves facts, news, current events, prices, people, releases, statistics or anything you are not certain about. Verify claims before confirming them. Never invent sources.
- Be direct and concise: a few short paragraphs at most, unless someone explicitly asks for detail. Light humour is welcome, but correctness comes first.
- If a claim is false or misleading, say so plainly and explain why.
- Reply in the language of the person who tagged you (often Slovenian or English).
- Format for Discord markdown. Do not add a sources list yourself; sources are appended automatically.
- Never ping @everyone or @here.`;
}

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
  partials: [Partials.Channel],
});

client.once(Events.ClientReady, (c) => {
  console.log(`Logged in as ${c.user.tag} using ${MISTRAL_MODEL}`);
});

client.on(Events.MessageCreate, async (message) => {
  if (message.author.bot || !client.user) return;
  if (!isSummoned(message)) return;

  const stopTyping = keepTyping(message);
  try {
    const answer = await ask({
      apiKey: MISTRAL_API_KEY,
      model: MISTRAL_MODEL,
      instructions: instructions(client.user.username),
      inputs: [{ role: "user", content: await buildPrompt(message) }],
    });
    await sendReply(message, answer);
  } catch (error) {
    console.error(error);
    await message
      .reply({
        content: "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek.",
        allowedMentions: { repliedUser: false },
      })
      .catch(() => {});
  } finally {
    stopTyping();
  }
});

function isSummoned(message: Message): boolean {
  const me = client.user!;
  if (!message.guild) return true; // DMs always get an answer
  if (message.mentions.users.has(me.id)) return true;
  // Tagging the bot's managed role from autocomplete also counts.
  return message.mentions.roles.some((role) => role.tags?.botId === me.id);
}

function keepTyping(message: Message): () => void {
  const channel = message.channel;
  if (!("sendTyping" in channel)) return () => {};
  const send = () => channel.sendTyping().catch(() => {});
  send();
  const interval = setInterval(send, 8_000);
  return () => clearInterval(interval);
}

function describe(message: Message): string {
  const name = message.member?.displayName ?? message.author.displayName;
  const parts = [message.cleanContent];
  for (const attachment of message.attachments.values()) {
    parts.push(`[attachment: ${attachment.name} ${attachment.url}]`);
  }
  for (const embed of message.embeds) {
    const text = [embed.title, embed.description, embed.url].filter(Boolean).join(" — ");
    if (text) parts.push(`[embed: ${text}]`);
  }
  return `${name}: ${parts.join(" ").trim()}`;
}

async function replyChain(message: Message): Promise<Message[]> {
  const chain: Message[] = [];
  let current = message;
  while (current.reference?.messageId && chain.length < REPLY_CHAIN_DEPTH) {
    const parent = await current.fetchReference().catch(() => null);
    if (!parent) break;
    chain.unshift(parent);
    current = parent;
  }
  return chain;
}

async function recentHistory(message: Message, exclude: Set<string>): Promise<Message[]> {
  if (CONTEXT_MESSAGES <= 0) return [];
  const fetched = await message.channel
    .messages.fetch({ before: message.id, limit: CONTEXT_MESSAGES })
    .catch(() => null);
  if (!fetched) return [];
  return [...fetched.values()].filter((m) => !exclude.has(m.id)).reverse();
}

async function buildPrompt(message: Message): Promise<string> {
  const chain = await replyChain(message);
  const history = await recentHistory(message, new Set(chain.map((m) => m.id)));

  const sections: string[] = [];
  if (history.length) {
    sections.push(`Recent messages in the channel (oldest first):\n${history.map(describe).join("\n")}`);
  }
  if (chain.length) {
    sections.push(`The tagged message is a reply to this thread (oldest first):\n${chain.map(describe).join("\n")}`);
  }
  sections.push(`Message that tagged you:\n${describe(message)}`);
  return sections.join("\n\n");
}

function formatAnswer(answer: Answer): string {
  let text = answer.text || "Hmm, na to nimam odgovora.";
  const sources = answer.sources.slice(0, MAX_SOURCES);
  if (sources.length) {
    // <url> stops Discord from unfurling every link into an embed.
    const links = sources.map((s) => `[${s.title.replace(/[\[\]]/g, "")}](<${s.url}>)`);
    text += `\n-# Viri: ${links.join(" · ")}`;
  }
  return text;
}

function split(text: string): string[] {
  const chunks: string[] = [];
  let rest = text;
  while (rest.length > DISCORD_LIMIT) {
    let cut = rest.lastIndexOf("\n", DISCORD_LIMIT);
    if (cut < DISCORD_LIMIT / 2) cut = rest.lastIndexOf(" ", DISCORD_LIMIT);
    if (cut < DISCORD_LIMIT / 2) cut = DISCORD_LIMIT;
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut).trimStart();
  }
  if (rest) chunks.push(rest);
  return chunks;
}

async function sendReply(message: Message, answer: Answer): Promise<void> {
  // Never let model output ping anyone; only notify the person who asked.
  const allowedMentions = { parse: [], repliedUser: true } as const;
  let last = message;
  for (const chunk of split(formatAnswer(answer))) {
    last = await last.reply({ content: chunk, allowedMentions });
  }
}

client.login(DISCORD_TOKEN);
