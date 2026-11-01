// Renders docs/assets/diagrams/<name>.excalidraw to docs/assets/<name>.svg
// with Excalidraw's own exportToSvg, in headless Chromium, and checks that
// every SVG draws its letters with no network (GitHub shows an SVG as <img>,
// which loads nothing from outside the file). scripts/make_diagrams.sh runs
// it in the Playwright image; see there for the options.
//
//   node render.mjs --diagrams DIR --assets DIR [--from-scenes] [--previews DIR] [names...]
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build as esbuild } from "esbuild";
import { chromium } from "playwright";
import { scenes } from "./scenes.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name) => {
  const i = args.indexOf(name);
  if (i < 0) return null;
  const [v] = args.splice(i, 2).slice(1);
  return v;
};
const fromScenes = args.includes("--from-scenes") && (args.splice(args.indexOf("--from-scenes"), 1), true);
const DIAGRAMS = opt("--diagrams");
const ASSETS = opt("--assets");
const PREVIEWS = opt("--previews");
const names = args.length ? args : Object.keys(scenes);
if (!DIAGRAMS || !ASSETS) throw new Error("usage: render.mjs --diagrams DIR --assets DIR [--from-scenes] [--previews DIR] [names...]");
for (const n of names) if (!scenes[n]) throw new Error(`no scene ${n} in scenes.mjs`);

// Excalidraw for the browser, from node_modules: one ES module and its chunks.
const BUNDLE = fs.mkdtempSync("/tmp/diagrams-bundle-");
await esbuild({
  entryPoints: [path.join(HERE, "entry.js")],
  bundle: true, format: "esm", splitting: true, minify: true,
  outdir: BUNDLE, logLevel: "error",
  define: { "process.env.NODE_ENV": '"production"', "process.env.IS_PREACT": '"false"' },
});
const EXCALIDRAW = path.join(HERE, "node_modules/@excalidraw/excalidraw/dist/prod");
const ORIGIN = "http://diagrams.local";

const browser = await chromium.launch();
const failures = [];
try {
  // The renderer reaches nothing but its own files: the bundle, and
  // Excalidraw's fonts (EXCALIDRAW_ASSET_PATH). Any other request is refused,
  // so a font the SVG would need from a CDN cannot slip in.
  const context = await browser.newContext();
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname === "/") {
      return route.fulfill({ contentType: "text/html", body:
        `<!doctype html><meta charset="utf-8"><body><script>window.EXCALIDRAW_ASSET_PATH="${ORIGIN}/assets/";</script>` +
        `<script type="module" src="/bundle/entry.js"></script></body>` });
    }
    const [, root, ...rest] = url.pathname.split("/");
    const base = root === "bundle" ? BUNDLE : root === "assets" ? EXCALIDRAW : null;
    const file = base && path.join(base, ...rest);
    if (!file || !file.startsWith(base) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: "" });
    return route.fulfill({ path: file });
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => console.error("page:", e.message));
  await page.goto(ORIGIN + "/");
  await page.waitForFunction(() => window.K_READY === true);

  const chars = [...new Set(names.flatMap((n) => scenes[n].elements)
    .flatMap((e) => [e.text, e.label && e.label.text]).filter(Boolean).join(""))].join("");
  await page.evaluate((c) => window.K.loadFonts(c), chars);

  fs.mkdirSync(DIAGRAMS, { recursive: true });
  for (const name of names) {
    const scene = scenes[name];
    const source = path.join(DIAGRAMS, `${name}.excalidraw`);
    if (fromScenes) {
      const json = await page.evaluate(({ els }) => window.K.toFile(window.K.build(els)), { els: scene.elements });
      fs.writeFileSync(source, json + "\n");
    }
    const svg = await page.evaluate(([json, o]) => window.K.render(json, o),
      [fs.readFileSync(source, "utf8"), { padding: 36, title: scene.title }]);
    const out = path.join(ASSETS, `${name}.svg`);
    fs.writeFileSync(out, svg + "\n");

    // The fonts are in the file: an @font-face whose source is a data URI, and
    // nothing fetched from anywhere.
    const faces = svg.match(/@font-face\s*{[^}]*}/g) || [];
    const families = faces.map((f) => (f.match(/font-family:\s*"?([^";]+)/) || [])[1]);
    if (!families.includes("Excalifont")) failures.push(`${name}: no Excalifont @font-face in the SVG`);
    if (faces.some((f) => !/src:\s*url\(data:font\/woff2;base64,/.test(f))) failures.push(`${name}: a font that is not a data URI`);
    if (/url\((?!data:|#)/.test(svg) || /(href|src)="https?:/.test(svg)) failures.push(`${name}: an external reference`);
    if (families.some((f) => f !== "Excalifont")) failures.push(`${name}: a fallback font is inlined (${families}): a character Excalifont has not`);
    console.log(`docs/assets/${name}.svg  ${(fs.statSync(out).size / 1024).toFixed(0)} KB  fonts: ${families.join(", ")}`);
  }

  // As GitHub shows them: an <img> on a page with the network off. The same
  // SVG with its @font-face removed must look different, or the letters were
  // never drawn with the embedded font.
  const offline = await browser.newContext({ offline: true, deviceScaleFactor: 2 });
  await offline.route("**/*", (route) => (route.request().url().startsWith("data:") ? route.continue() : route.abort()));
  const view = await offline.newPage();
  for (const name of names) {
    const svg = fs.readFileSync(path.join(ASSETS, `${name}.svg`), "utf8");
    const shot = async (s) => {
      // 880 px wide, the README's column, on GitHub's dark background.
      await view.setContent(`<!doctype html><body style="margin:0"><div id="w" style="display:inline-block;padding:24px;background:#0d1117">` +
        `<img id="d" style="display:block;width:880px" src="data:image/svg+xml;base64,${Buffer.from(s).toString("base64")}"></div></body>`);
      await view.waitForFunction(() => document.getElementById("d").complete);
      await view.waitForTimeout(300);
      return view.locator("#w").screenshot();
    };
    const withFont = await shot(svg);
    const without = await shot(svg.replace(/@font-face\s*{[^}]*}/g, ""));
    if (withFont.equals(without)) failures.push(`${name}: offline, the <img> looks the same without its @font-face`);
    else console.log(`${name}: offline <img> drawn with the embedded font (differs from the font stripped)`);
    if (PREVIEWS) {
      fs.mkdirSync(PREVIEWS, { recursive: true });
      fs.writeFileSync(path.join(PREVIEWS, `${name}.png`), withFont);
    }
  }
} finally {
  await browser.close();
  fs.rmSync(BUNDLE, { recursive: true, force: true });
}
if (failures.length) {
  console.error("FAILED:\n  " + failures.join("\n  "));
  process.exit(1);
}
