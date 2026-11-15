// What the screen sends a browser that has not been told about a part of
// the Cloud's product: run with node after `npm ci` (`npm test`).
//
// A part served to a list of accounts (korely_agent/core/extensions.py,
// Part.accounts) must be invisible to anybody it is not served to, until
// it is served to everybody (the founder's rule of 13 Nov 2026). The server
// answers its routes as routes that do not exist; the screen loads the
// parts' chunk once the server names a part for the signed-in account
// (lib/cloud.ts, useScreenParts), or at the start on an address of the
// shape of a source's return (below). Until 13 Nov 2026 the main chunk, which
// the login page loads, carried every part's name, path and the file names
// of its pages (the skeptic's finding: lib/cloud.ts loaded them eagerly).
//
// This builds the screen as `vite build` does, in memory, and reads every
// chunk but the parts' own: none may carry a part's label or path (read
// from the parts themselves, src/hosted/parts.ts) or the file name of one
// of the parts' chunks. The parts' own chunks are those made only of the
// files of a part's folder (src/hosted/<folder>/, beside its part.ts) and
// the parts' index (src/hosted/parts.ts). A build without src/hosted/ (the
// public repository) has no part and passes.
//
// What stays true, and what this does not check: the parts' chunks are
// files of the build, as every chunk is, and a person who reads the main
// chunk finds the index's file name and can fetch it. The start asks the
// parts' chunk on any address that ends in `/callback` (src/lib/returns.ts:
// the shape of a source's return is all the main chunk knows, and it knows
// it before the session), so whoever opens an address of that shape, signed
// out or signed in to an account served none, loads the parts' names. What
// this checks is the main chunk and every chunk but the parts' own: none
// of them names a part.
//
// Exits non-zero on a chunk that names a part.
import assert from "node:assert/strict";
import { existsSync, readdirSync } from "node:fs";
import path from "node:path";
import { build, createServer } from "vite";

const root = process.cwd();
const hosted = path.join(root, "src", "hosted");
const folders = existsSync(hosted)
  ? readdirSync(hosted, { withFileTypes: true })
      .filter((e) => e.isDirectory() && existsSync(path.join(hosted, e.name, "part.ts")))
      .map((e) => path.join(hosted, e.name) + path.sep)
  : [];
if (folders.length === 0) {
  console.log("no part of the Cloud in this build: nothing to check");
  process.exit(0);
}

const vite = await createServer({
  appType: "custom",
  logLevel: "error",
  server: { middlewareMode: true, hmr: false, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
});
let parts;
try {
  ({ parts } = await vite.ssrLoadModule("/src/hosted/parts.ts"));
} finally {
  await vite.close();
}
assert.equal(parts.length, folders.length, "every folder with a part.ts is one of the parts");

const out = await build({ logLevel: "error", build: { write: false } });
const chunks = (Array.isArray(out) ? out[0] : out).output.filter((o) => o.type === "chunk");
const index = path.join(hosted, "parts.ts");
const theirs = (c) => {
  const files = Object.keys(c.modules).filter((m) => !m.includes("node_modules"));
  return files.length > 0 && files.every((m) => m === index || folders.some((f) => m.startsWith(f)));
};
const partsChunks = chunks.filter(theirs);
const hasIndex = (c) => Object.keys(c.modules).includes(index);
assert.ok(partsChunks.some(hasIndex), "the parts' index is in a chunk of the parts");
const others = chunks.filter((c) => !theirs(c));
assert.ok(others.some((c) => c.isEntry), "the main chunk is read");

const words = [
  ...parts.flatMap((p) => [p.label, p.path, `${p.path}/`]),
  ...partsChunks.filter((c) => !hasIndex(c)).map((c) => path.basename(c.fileName)),
];
let failed = 0;
for (const c of others) {
  for (const word of words) {
    const at = c.code.indexOf(word);
    if (at < 0) continue;
    failed += 1;
    console.error(`FAIL ${c.fileName} names ${JSON.stringify(word)}: …${c.code.slice(Math.max(0, at - 40), at + 40)}…`);
  }
}
console.log(`${others.length} chunks read, ${partsChunks.length} of the parts left out, ${words.length} names looked for`);
if (failed) process.exit(1);
