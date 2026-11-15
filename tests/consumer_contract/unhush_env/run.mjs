// Execute the unmodified pinned consumer, with browser globals supplied by a private fixture.
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const prefix = process.env.UNHUSH_CONTRACT_PREFIX;
if (!prefix) throw new Error("UNHUSH_CONTRACT_PREFIX is required");
const lock = JSON.parse(await readFile(join(dirname(fileURLToPath(import.meta.url)), "source-lock.json"), "utf8"));
const receipt = JSON.parse(await readFile(join(prefix, "client-manifest.json"), "utf8"));
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
if (receipt.revision !== lock.revision || receipt.version !== lock.version) throw new Error("wrong client snapshot");
for (const [name, digest] of Object.entries(lock.files)) {
  if (hash(await readFile(join(prefix, "upstream", name))) !== digest) throw new Error("source integrity mismatch");
}
const bundle = join(prefix, "client.mjs");
if (hash(await readFile(bundle)) !== receipt.bundle_sha256) throw new Error("bundle integrity mismatch");
let inputText = "";
for await (const chunk of process.stdin) {
  inputText += chunk;
  if (Buffer.byteLength(inputText) > 128 * 1024) throw new Error("input bound exceeded");
}
const input = JSON.parse(inputText);
const values = new Map(Object.entries({
  unhush_provider: "none", unhush_llm_provider: "custom",
  unhush_llm_custom_url: input.url, unhush_llm_custom_key: input.token,
  unhush_llm_model_custom: "codex", unhush_llm_custom_start_cmd: "",
  unhush_llm_system_prompt: "Format the transcript without changing its meaning.",
  ...input.settings,
}));
globalThis.localStorage = {getItem: key => values.get(key) ?? null};
const warnings = [];
globalThis.window = {electronAPI: {
  setFormatterWarning: (reason, enabled) => warnings.push({reason, enabled}),
  setTranscriptionWarning: () => {throw new Error("unexpected STT path");},
  log: () => {},
  runCommand: () => {throw new Error("unexpected Start Command");},
}};
const deadlines = [];
const nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = milliseconds => {deadlines.push(milliseconds); return nativeTimeout(milliseconds);};
const requests = [];
let blocked = 0;
const nativeFetch = globalThis.fetch;
const allowedOrigin = new URL(input.url).origin;
globalThis.fetch = async (url, options = {}) => {
  const parsed = new URL(url);
  if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" || parsed.origin !== allowedOrigin) {
    blocked += 1;
    throw new Error("non-loopback egress refused");
  }
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({path: parsed.pathname, method: options.method ?? "GET", body});
  if (input.injectTimeout) throw new DOMException("fixture timeout", "TimeoutError");
  return nativeFetch(url, options);
};
const nativeNow = Date.now.bind(Date);
let offset = 0;
Date.now = () => nativeNow() + offset;
const client = await import(pathToFileURL(bundle).href);
const results = [];
for (const action of input.actions ?? [{operation: "format"}]) {
  try {
    if (action.operation === "advanceClock") {
      offset += action.milliseconds;
      results.push({ok: true, operation: "advanceClock"});
    } else if (action.operation === "models") {
      const data = await client.fetchModels(client.getBaseUrl(input.url), input.token);
      results.push({ok: data !== null, models: data?.data.map(model => model.id) ?? []});
    } else if (action.operation === "warmup") {
      await client.ensureCustomServices(() => {}, true);
      const until = nativeNow() + 5000;
      while (client.getLLMWarmupStatus() === "pending" && nativeNow() < until) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      const status = client.getLLMWarmupStatus();
      results.push({ok: status === "ready", status});
    } else if (action.operation === "format") {
      const config = client.getLLMConfig();
      const output = await client.postProcessTranscript(action.transcript ?? "A synthetic transcript.", config);
      results.push({ok: true, content: output.content});
    } else {
      throw new Error("unknown test operation");
    }
  } catch (error) {
    results.push({ok: false, error: error.message});
  }
}
console.log(JSON.stringify({version: receipt.version, revision: receipt.revision, results,
  requests, deadlines, blocked, warnings, startCommand: "disabled", transcription: "disabled"}));
