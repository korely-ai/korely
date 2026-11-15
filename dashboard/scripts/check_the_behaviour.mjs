// What the screen's modules do, run with node: `npm test` (after `npm ci`),
// beside check_the_reads.mjs.
//
// The dashboard has no test runner of its own and no DOM in node, and the
// repository's tests read its source as text. On 13 Nov 2026 a review
// mutated a page of the screen twenty times, and eighteen changes went
// through every test, the type check and the build: an OAuth `code` left in
// the address bar, a once-only guard off, a retry offered for a request that
// can no longer succeed, a token copied into the page's state, a code
// written to window.name. A source that reads right is not a page that
// behaves right.
//
// So the modules whose behaviour matters are written to be run: what they do
// is a function of what they are handed (a clock, the address, the routes),
// and each has a `*.behaviour.ts` (or `.tsx`) beside it that hands them
// stand-ins and looks at what they did (src/lib/behaviour.ts has the
// stand-ins). This loads each through Vite, as the build compiles it, and
// runs every check it exports (`CHECKS`). The files are found twice, from the
// compiler's own list of the source (tsconfig.json) and by walking src/, and
// the two lists must agree, so a check file that the walk or the list drops
// fails instead of checking nothing.
//
// A check that has not ended within CHECK_MS fails: a promise that never
// settles (a page left on "Finishing…") is a failure, not a hang.
//
// Exits non-zero if any check fails, after running them all.
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { createServer } from "vite";

const root = process.cwd();
const CHECK_MS = 10_000;
const isCheck = (f) => /\.behaviour\.tsx?$/.test(f);

const config = ts.getParsedCommandLineOfConfigFile(
  path.join(root, "tsconfig.json"), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} });
const listed = config.fileNames.filter(isCheck).map((f) => path.relative(root, f)).sort();
const walked = [];
const walk = (dir) => {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (isCheck(e.name)) walked.push(path.relative(root, p));
  }
};
walk(path.join(root, "src"));
walked.sort();

const vite = await createServer({
  appType: "custom",
  logLevel: "error",
  server: { middlewareMode: true, hmr: false, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
});
let failed = 0;
let ran = 0;
try {
  assert.deepEqual(walked, listed, "every behaviour file under src/ is one the compiler checks, and the other way round");
  assert.ok(listed.length > 0, "the behaviour files are found");
  for (const file of listed) {
    const { CHECKS } = await vite.ssrLoadModule(`/${file.split(path.sep).join("/")}`);
    const names = Object.keys(CHECKS ?? {});
    assert.ok(names.length > 0, `${file}: exports its CHECKS`);
    for (const name of names) {
      ran += 1;
      try {
        let timer;
        await Promise.race([
          CHECKS[name](assert),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error(`did not end within ${CHECK_MS / 1000} s`)), CHECK_MS);
          }),
        ]).finally(() => clearTimeout(timer));
        console.log(`ok   ${file}: ${name}`);
      } catch (e) {
        failed += 1;
        console.error(`FAIL ${file}: ${name}\n     ${String(e?.message ?? e).split("\n").join("\n     ")}`);
      }
    }
  }
  console.log(`${ran - failed} of ${ran} behaviour checks pass, in ${listed.length} files`);
} catch (e) {
  failed += 1;
  console.error(`FAIL ${e.message}`);
} finally {
  await vite.close();
}
process.exit(failed ? 1 : 0);
