// The README's diagrams as Excalidraw element skeletons
// (convertToExcalidrawElements). `bash scripts/make_diagrams.sh --from-scenes`
// turns them into docs/assets/diagrams/<name>.excalidraw, overwriting those
// files; without it the .excalidraw files are the source, so a diagram edited
// on excalidraw.com and saved over its file is rendered as it is.
//
// Every claim here is the product's: docker-compose.yml and install.sh for
// what runs, README.md and docs/guide/ for the write and read paths and the keys.

const INK = "#1e1e1e";
const FONT = 5; // Excalifont

// One palette for every diagram, a shade brighter than Excalidraw's pastels.
const C = {
  korely: { stroke: "#099268", fill: "#63e6be" },   // Korely: teal
  agent: { stroke: "#1971c2", fill: "#74c0fc" },    // agents and their keys: blue
  app: { stroke: "#e8590c", fill: "#ffc078" },      // your app: orange
  data: { stroke: "#6741d9", fill: "#b197fc" },     // Postgres, projects, data: purple
  closed: { stroke: "#868e96", fill: "#dee2e6" },   // closed and past facts: grey
  provider: { stroke: "#f08c00", fill: "#ffe066" }, // the model provider: yellow
  you: { stroke: "#c2255c", fill: "#faa2c1" },      // you, signed in: pink
  frame: { stroke: "#495057", fill: "transparent" },
  no: "#e03131",
  note: "#495057",
};

const label = (text, size = 24, o = {}) => ({
  text, fontSize: size, fontFamily: FONT, strokeColor: o.color || INK,
  textAlign: o.align || "center", verticalAlign: o.valign || "middle",
});

function box(id, x, y, width, height, c, text, o = {}) {
  return {
    type: o.shape || "rectangle", id, x, y, width, height,
    strokeColor: c.stroke, backgroundColor: c.fill, fillStyle: o.fillStyle || "solid",
    strokeWidth: o.strokeWidth || 2, roughness: 1,
    strokeStyle: o.dashed ? "dashed" : "solid",
    roundness: o.shape === "ellipse" ? null : { type: 3 },
    ...(text ? { label: label(text, o.size, o) } : {}),
  };
}

// Text centred (or aligned) in a box nobody sees.
function words(id, x, y, width, height, text, size = 24, o = {}) {
  return box(id, x, y, width, height, { stroke: "transparent", fill: "transparent" }, text,
             { size, ...o });
}

function arrow(id, pts, o = {}) {
  const [x0, y0] = pts[0];
  const points = pts.map(([x, y]) => [x - x0, y - y0]);
  return {
    type: o.line ? "line" : "arrow", id, x: x0, y: y0, points,
    width: Math.max(...points.map((p) => Math.abs(p[0]))),
    height: Math.max(...points.map((p) => Math.abs(p[1]))),
    strokeColor: o.color || INK, strokeWidth: o.strokeWidth || 2, roughness: 1,
    strokeStyle: o.dashed ? "dashed" : "solid",
    ...(o.line ? {} : { startArrowhead: o.both ? "arrow" : null, endArrowhead: "arrow" }),
    ...(o.label ? { label: label(o.label, o.size || 20, { color: o.labelColor }) } : {}),
  };
}

// 1. One memory, every agent ------------------------------------------------
const sharedMemory = (() => {
  const W = 1100;
  const left = ["Claude Code", "Cursor", "ChatGPT"];
  const right = ["Gemini CLI", "Codex", "Hermes"];
  const ys = [130, 250, 370];
  const ky = [220, 285, 350];
  const els = [words("title", 0, 0, W, 60, "One memory, every agent", 40)];
  left.forEach((n, i) => {
    els.push(box(`l${i}`, 0, ys[i], 240, 72, C.agent, n, { size: 26 }));
    els.push(arrow(`la${i}`, [[244, ys[i] + 36], [376, ky[i]]], { both: true, label: "MCP" }));
  });
  right.forEach((n, i) => {
    els.push(box(`r${i}`, 860, ys[i], 240, 72, C.agent, n, { size: 26 }));
    els.push(arrow(`ra${i}`, [[856, ys[i] + 36], [724, ky[i]]], { both: true, label: "MCP" }));
  });
  els.push(
    box("korely", 380, 170, 340, 230, C.korely, null, { strokeWidth: 3 }),
    words("korely-name", 380, 190, 340, 60, "Korely Agent", 36),
    words("korely-what", 380, 255, 340, 130, "one memory,\nwith dates,\nin your Postgres", 26),
    box("app", 400, 490, 300, 86, C.app, "Your app\nPython, JS", { size: 26 }),
    arrow("app-a", [[550, 486], [550, 404]], { both: true, label: "SDK, REST" }),
    words("caption", 0, 610, W, 50, "What one agent saves, every other one can read.", 30),
  );
  return { title: "One memory, every agent", elements: els };
})();

// 2. Facts with dates: customer 4821 ----------------------------------------
const factsWithDates = (() => {
  const W = 1100;
  const AXIS = 330;
  // 3 px a day: 15 Mar 2026 at 230, 1 Apr at 281, 20 Jun at 521, 30 Oct (today) at 917.
  const X = { mar15: 230, apr1: 281, jun20: 521, today: 917 };
  const els = [
    words("title", 0, 0, W, 60, "Customer 4821: facts with dates", 38),
    words("closed-note", X.jun20 - 260, 82, 520, 56, "closed on 20 Jun 2026, not deleted", 26, { color: C.no }),
    arrow("closed-a", [[X.jun20, 140], [X.jun20 - 4, 186]], { color: C.no }),
    box("priority", X.mar15, 190, X.jun20 - X.mar15, 70, C.closed, "Priority Support plan", { size: 26 }),
    box("basic", X.jun20, 190, 1070 - X.jun20, 70, C.korely, "Basic plan, still open", { size: 26 }),
    arrow("axis", [[40, AXIS], [1080, AXIS]], { strokeWidth: 2 }),
    words("axis-name", 1000, AXIS + 8, 80, 34, "time", 22, { color: C.note, align: "right" }),
    arrow("g1", [[X.mar15, 262], [X.mar15, AXIS]], { line: true, dashed: true, color: C.closed.stroke }),
    arrow("g2", [[X.jun20, 262], [X.jun20, AXIS]], { line: true, dashed: true, color: C.closed.stroke }),
    words("d1", 30, AXIS + 10, X.mar15 - 38, 34, "15 Mar 2026", 22, { align: "right" }),
    words("d2", X.jun20 - 90, AXIS + 10, 180, 34, "20 Jun 2026", 22),
    words("d3", X.today - 60, AXIS + 10, 120, 34, "today", 22),
    box("read-then", 30, 460, 400, 100, C.agent, "as of 1 April 2026:\nPriority Support plan", { size: 26 }),
    arrow("read-then-a", [[X.apr1, 456], [X.apr1, AXIS + 6]], { label: "reads" }),
    box("read-now", 700, 460, 370, 100, C.agent, "today:\nBasic plan", { size: 26 }),
    arrow("read-now-a", [[X.today, 456], [X.today, AXIS + 48]], { label: "reads" }),
    box("clock", 30, 600, 1040, 56, { stroke: C.note, fill: "transparent" },
        "and a second clock: known_as_of reads what the store knew on a date",
        { size: 22, dashed: true, color: C.note }),
  ];
  return { title: "Customer 4821: facts with dates", elements: els };
})();

// 3. What the installer starts ----------------------------------------------
// docker-compose.yml's services db, facts-dir and api (install.sh runs
// `up -d api`: never Caddy), the volume pgdata, the port 127.0.0.1:${PORT:-8000}.
const whatRuns = (() => {
  const els = [
    words("title", 0, 0, 1300, 56, "What the installer starts on your machine", 36),
    box("machine", 0, 76, 900, 690, C.frame, null, { dashed: true }),
    words("machine-name", 18, 90, 220, 36, "your machine", 24, { align: "left", color: C.note }),
    box("browser", 470, 130, 200, 70, C.you, "your browser", { size: 24 }),
    box("agents", 690, 130, 190, 70, C.agent, "your agents", { size: 24 }),
    box("docker", 30, 270, 840, 350, C.frame, null, { dashed: true }),
    words("docker-name", 48, 280, 160, 36, "Docker", 24, { align: "left", color: C.note }),
    box("db", 60, 330, 280, 110, C.data, "Postgres + pgvector\n(db)", { size: 24 }),
    box("pgdata", 60, 510, 280, 80, C.data, "volume pgdata", { size: 22, dashed: true, fillStyle: "hachure" }),
    arrow("db-data", [[200, 444], [200, 506]], { label: "data" }),
    box("api", 540, 320, 300, 150, C.korely, "Korely Agent (api)\ndashboard, API, MCP\nport 8000", { size: 24, strokeWidth: 3 }),
    arrow("api-db", [[536, 385], [344, 385]], { both: true, label: "SQL" }),
    box("facts-dir", 540, 520, 300, 76, C.closed, "facts-dir\nruns once, then exits", { size: 20, dashed: true }),
    arrow("facts-dir-a", [[690, 600], [690, 664]], { label: "prepares" }),
    box("facts", 540, 668, 300, 64, { stroke: C.note, fill: "#ffffff" }, "the ./facts folder", { size: 22 }),
    arrow("browser-a", [[570, 204], [610, 316]], { label: "127.0.0.1:8000" }),
    arrow("agents-a", [[785, 204], [770, 316]], { label: "127.0.0.1:8000" }),
    box("provider", 1080, 330, 210, 110, C.provider, "model provider\n(your key)", { size: 24 }),
    arrow("provider-a", [[844, 385], [1076, 385]], { both: true, label: "only to\nwrite facts" }),
    words("provider-note", 1045, 460, 280, 110,
          "searches never call it:\nvectors are computed\nhere (the default)", 20, { color: C.note }),
  ];
  return { title: "What the installer starts on your machine", elements: els };
})();

// 4. How it works: the write path and the read path -------------------------
const howItWorks = (() => {
  const els = [
    words("title", 0, 0, 1240, 56, "How it works", 38),
    words("write", 0, 70, 200, 44, "Write", 32, { align: "left", color: C.korely.stroke }),
    box("w-agent", 0, 130, 200, 96, C.agent, "an agent saves\na memory", { size: 24 }),
    arrow("w1", [[204, 178], [286, 178]], {}),
    box("w-db", 290, 130, 250, 96, C.data, "stored, and\nsearchable at once", { size: 24 }),
    arrow("w2", [[544, 178], [626, 178]], {}),
    box("w-worker", 630, 130, 210, 96, C.korely, "a worker,\nin the background", { size: 22 }),
    arrow("w3", [[844, 160], [936, 160]], { label: "text" }),
    arrow("w4", [[936, 200], [844, 200]], { label: "facts" }),
    box("w-provider", 940, 120, 300, 116, C.provider, "model provider\n(your key)\nreads facts, judges them", { size: 22 }),
    arrow("w5", [[735, 230], [735, 316]], { label: "the judge's answer" }),
    box("w-close", 540, 320, 430, 110, C.data,
        "new fact saved; an older fact\nabout the same thing is closed,\nnever deleted", { size: 22 }),
    words("w-nokey", 0, 330, 500, 90, "No key? Memories are still stored\nand searchable; facts wait for one.",
          20, { align: "left", color: C.note }),
    arrow("sep", [[0, 470], [1240, 470]], { line: true, dashed: true, color: C.closed.stroke }),
    words("read", 0, 490, 200, 44, "Read", 32, { align: "left", color: C.agent.stroke }),
    box("r-agent", 0, 550, 200, 96, C.agent, "an agent asks:\nsearch, context", { size: 24 }),
    arrow("r1", [[204, 598], [286, 598]], {}),
    box("r-korely", 290, 550, 320, 96, C.korely, "one vector of the question,\ncomputed on your server", { size: 22 }),
    words("r-default", 290, 650, 320, 30, "(by default, with a local model)", 18, { color: C.note }),
    arrow("r2", [[614, 598], [696, 598]], { label: "SQL" }),
    box("r-db", 700, 550, 280, 96, C.data, "everything else is SQL:\nmemories, facts, dates", { size: 22 }),
    arrow("r3", [[840, 650], [840, 712], [100, 712], [100, 650]], { label: "the answer" }),
    words("r-nomodel", 1000, 550, 240, 96, "No model\non reads", 32, { color: C.korely.stroke }),
  ];
  return { title: "How it works: the write path and the read path", elements: els };
})();

// 5. Who reads what ------------------------------------------------------------
const whoReadsWhat = (() => {
  const els = [
    words("title", 0, 0, 1100, 56, "Projects are walls", 38),
    box("account", 0, 76, 1100, 650, C.frame, null, { dashed: true }),
    words("account-name", 18, 90, 220, 36, "your account", 24, { align: "left", color: C.note }),
    box("k-shop", 60, 130, 300, 72, C.agent, "key of project Shop", { size: 24 }),
    box("k-clinic", 740, 130, 300, 72, C.agent, "key of project Clinic", { size: 24 }),
    box("p-shop", 60, 290, 300, 140, C.data, "project Shop\nits memories, its facts", { size: 22 }),
    box("p-clinic", 740, 290, 300, 140, C.data, "project Clinic\nits memories, its facts", { size: 22 }),
    arrow("a-shop", [[210, 206], [210, 286]], { both: true, label: "reads, writes" }),
    arrow("a-clinic", [[890, 206], [890, 286]], { both: true, label: "reads, writes" }),
    box("wall", 525, 240, 50, 230, { stroke: C.closed.stroke, fill: "#868e96" }, null,
        { fillStyle: "cross-hatch", strokeWidth: 2 }),
    arrow("no-shop", [[364, 180], [518, 300]], { dashed: true, color: C.no, label: "no", labelColor: C.no, size: 24 }),
    arrow("no-clinic", [[736, 180], [582, 300]], { dashed: true, color: C.no, label: "no", labelColor: C.no, size: 24 }),
    box("admin", 220, 560, 660, 130, C.you,
        "an admin key, or you signed in to the dashboard:\nevery project, and the keys,\nthe webhooks and the audit trail", { size: 24 }),
    arrow("admin-shop", [[330, 556], [240, 434]], { both: true, label: "reads, writes" }),
    arrow("admin-clinic", [[770, 556], [860, 434]], { both: true, label: "reads, writes" }),
  ];
  return { title: "Who reads what: projects are walls", elements: els };
})();

export const scenes = {
  "shared-memory": sharedMemory,
  "facts-with-dates": factsWithDates,
  "what-runs": whatRuns,
  "how-it-works": howItWorks,
  "who-reads-what": whoReadsWhat,
};
