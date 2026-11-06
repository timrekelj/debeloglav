const API_URL = "https://api.mistral.ai/v1/conversations";

export type InputMessage = {
  role: "user" | "assistant";
  content: string;
};

export type Source = {
  title: string;
  url: string;
};

export type Answer = {
  text: string;
  sources: Source[];
  searched: boolean;
};

type Chunk =
  | { type: "text"; text: string }
  | { type: "tool_reference"; tool: string; title: string; url?: string; source?: string }
  | { type: string; [key: string]: unknown };

type Output =
  | { type: "message.output"; content: string | Chunk[] }
  | { type: "tool.execution"; name: string }
  | { type: string; [key: string]: unknown };

type ConversationResponse = {
  outputs: Output[];
};

export async function ask(params: {
  apiKey: string;
  model: string;
  instructions: string;
  inputs: InputMessage[];
}): Promise<Answer> {
  const res = await fetch(API_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${params.apiKey}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      model: params.model,
      instructions: params.instructions,
      inputs: params.inputs,
      tools: [{ type: "web_search" }],
      completion_args: { temperature: 0.4 },
      store: false,
    }),
    signal: AbortSignal.timeout(120_000),
  });

  if (!res.ok) {
    throw new Error(`Mistral API ${res.status}: ${await res.text()}`);
  }

  const data = (await res.json()) as ConversationResponse;

  let text = "";
  const sources = new Map<string, Source>();
  let searched = false;

  for (const output of data.outputs) {
    if (output.type === "tool.execution") {
      searched = true;
      continue;
    }
    if (output.type !== "message.output") continue;

    const content = (output as Extract<Output, { type: "message.output" }>).content;
    if (typeof content === "string") {
      text += content;
      continue;
    }
    for (const chunk of content) {
      if (chunk.type === "text") {
        text += (chunk as { text: string }).text;
      } else if (chunk.type === "tool_reference") {
        const ref = chunk as { title: string; url?: string };
        if (ref.url && !sources.has(ref.url)) {
          sources.set(ref.url, { title: ref.title, url: ref.url });
        }
      }
    }
  }

  return { text: text.trim(), sources: [...sources.values()], searched };
}
