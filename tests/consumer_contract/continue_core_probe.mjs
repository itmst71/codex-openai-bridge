import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

function invariant(value, message) {
  if (!value) {
    throw new Error(message);
  }
}

const coreRoot = process.env.CONTINUE_CORE_ROOT;
const bridgeUrl = process.env.CONTINUE_BRIDGE_URL;
const bridgeToken = process.env.CONTINUE_BRIDGE_TOKEN;
invariant(coreRoot, "CONTINUE_CORE_ROOT is required");
invariant(bridgeUrl, "CONTINUE_BRIDGE_URL is required");
invariant(bridgeToken, "CONTINUE_BRIDGE_TOKEN is required");

const packageJson = JSON.parse(
  fs.readFileSync(path.join(coreRoot, "package.json"), "utf8"),
);
invariant(packageJson.version === "1.1.0", "unexpected Continue core version");

globalThis.__dirname = coreRoot;
const sourceUrl = pathToFileURL(
  path.join(coreRoot, "llm", "llms", "OpenAI.ts"),
).href;
const devDataUrl = pathToFileURL(
  path.join(coreRoot, "data", "devdataSqlite.ts"),
).href;
const { DevDataSqliteDb } = await import(devDataUrl);
DevDataSqliteDb.logTokensGenerated = async () => {};
const { default: ContinueOpenAI } = await import(sourceUrl);

const llm = new ContinueOpenAI({
  title: "Codex Bridge",
  model: "codex",
  apiBase: bridgeUrl,
  apiKey: bridgeToken,
  contextLength: 200000,
  useLegacyCompletionsEndpoint: false,
  roles: ["chat", "edit", "apply"],
  completionOptions: {
    model: "codex",
    maxTokens: 4096,
    stream: true,
  },
  requestOptions: {
    timeout: 3000,
  },
});

const signal = new AbortController().signal;
let chatText = "";
for await (const chunk of llm.streamChat(
  [{ role: "user", content: "bounded chat" }],
  signal,
  { log: false },
)) {
  chatText += chunk.content;
}
invariant(chatText === "continue chat", "unexpected Continue chat projection");

let editText = "";
for await (const chunk of llm.streamComplete(
  "bounded edit prompt",
  signal,
  { log: false },
)) {
  editText += chunk;
}
invariant(editText === "continue edit", "unexpected Continue edit projection");

const weatherTool = {
  type: "function",
  displayTitle: "Weather",
  function: {
    name: "weather",
    description: "Return bounded weather information for a city",
    parameters: {
      type: "object",
      properties: { city: { type: "string" } },
      required: ["city"],
      additionalProperties: false,
    },
    strict: true,
  },
};

const firstChunks = [];
for await (const chunk of llm.streamChat(
  [{ role: "user", content: "use weather" }],
  signal,
  {
    log: false,
    stream: false,
    tools: [weatherTool],
    toolChoice: { type: "function", function: { name: "weather" } },
  },
)) {
  firstChunks.push(chunk);
}
invariant(firstChunks.length === 1, "unexpected Continue tool-call chunk count");
const first = firstChunks[0];
invariant(first.toolCalls?.length === 1, "missing Continue tool call");
const call = first.toolCalls[0];
invariant(call.id === "call_weather_contract", "unexpected Continue tool call id");
invariant(call.function?.name === "weather", "unexpected Continue tool name");
invariant(
  call.function?.arguments === '{"city":"Tokyo"}',
  "unexpected Continue tool arguments",
);

const finalChunks = [];
for await (const chunk of llm.streamChat(
  [
    { role: "user", content: "use weather" },
    first,
    {
      role: "tool",
      content: "bounded weather result",
      toolCallId: call.id,
    },
  ],
  signal,
  {
    log: false,
    stream: false,
    tools: [weatherTool],
    toolChoice: "none",
  },
)) {
  finalChunks.push(chunk);
}
invariant(finalChunks.length === 1, "unexpected Continue final chunk count");
invariant(
  finalChunks[0].content === "continue tool complete",
  "unexpected Continue final projection",
);

await new Promise((resolve) => setTimeout(resolve, 250));
console.log(
  "CONTINUE_CONTRACT=" +
    JSON.stringify({
      chatStream: true,
      editStream: true,
      toolRoundtrip: true,
      packageVersion: packageJson.version,
    }),
);
