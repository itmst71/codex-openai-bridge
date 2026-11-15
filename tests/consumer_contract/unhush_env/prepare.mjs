// Optional test setup only: no bridge runtime dependency or real credential access.
import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir, lstat } from "node:fs/promises";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");
const here = dirname(fileURLToPath(import.meta.url));
const lock = JSON.parse(await readFile(join(here, "source-lock.json"), "utf8"));
const prefix = process.argv[2];
if (!prefix || !isAbsolute(prefix)) throw new Error("an absolute owned prefix is required");
const upstream = join(prefix, "upstream");
const files = Object.entries(lock.files);

async function existing(path) {
  try {
    const info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("source integrity: not a regular file");
    return await readFile(path);
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

// Check all cached inputs before network access; never silently replace a different snapshot.
for (const [relative, digest] of files) {
  const bytes = await existing(join(upstream, relative));
  if (bytes !== null && sha256(bytes) !== digest) throw new Error(`source integrity: ${relative}`);
}
for (const [relative, digest] of files) {
  const target = join(upstream, relative);
  if (await existing(target) !== null) continue;
  const url = `https://raw.githubusercontent.com/${lock.repository}/${lock.revision}/${relative}`;
  const response = await fetch(url, {signal: AbortSignal.timeout(30000), redirect: "error"});
  if (!response.ok || !response.body) throw new Error(`source download failed: ${response.status}`);
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 256 * 1024) throw new Error("source download exceeded its bound");
      chunks.push(Buffer.from(value));
    }
  } finally {
    await reader.cancel();
  }
  const bytes = Buffer.concat(chunks);
  if (sha256(bytes) !== digest) throw new Error(`source integrity: ${relative}`);
  await mkdir(dirname(target), {recursive: true, mode: 0o700});
  await writeFile(target, bytes, {flag: "wx", mode: 0o600});
}
const packageInfo = JSON.parse(await readFile(join(upstream, "package.json"), "utf8"));
if (packageInfo.name !== "unhush" || packageInfo.version !== lock.version) throw new Error("source package mismatch");
const output = join(prefix, "client.mjs");
const receipt = join(prefix, "client-manifest.json");
const oldReceipt = await existing(receipt);
if (oldReceipt !== null) {
  const info = JSON.parse(oldReceipt);
  const bytes = await existing(output);
  if (info.revision !== lock.revision || bytes === null || sha256(bytes) !== info.bundle_sha256) {
    throw new Error("prepared client integrity mismatch");
  }
  console.log(JSON.stringify({version: lock.version, revision: lock.revision, cached: true}));
} else {
  if (await existing(output) !== null) throw new Error("refusing an unowned existing client bundle");
  const compiler = await import(pathToFileURL(join(prefix, "node_modules/esbuild/lib/main.js")).href);
  if (compiler.version !== "0.28.1") throw new Error("wrong optional compiler version");
  const entry = `export * from "./upstream/src/audio/llmApi.ts";\n` +
    `export {fetchModels, ensureCustomServices, getLLMWarmupStatus, getBaseUrl, ` +
    `getRelevantConfigSnapshot, invalidateServiceContact} from "./upstream/src/audio/customModelService.ts";\n`;
  const result = await compiler.build({
    stdin: {contents: entry, resolveDir: prefix, loader: "js"},
    bundle: true, platform: "node", format: "esm", target: "node20", write: false,
    logLevel: "silent", charset: "utf8",
  });
  const bytes = result.outputFiles[0].contents;
  const info = {version: lock.version, revision: lock.revision, compiler: compiler.version,
    files: lock.files, bundle_sha256: sha256(bytes)};
  await writeFile(output, bytes, {flag: "wx", mode: 0o600});
  await writeFile(receipt, JSON.stringify(info, null, 2) + "\n", {flag: "wx", mode: 0o600});
  console.log(JSON.stringify({version: lock.version, revision: lock.revision, cached: false}));
}
