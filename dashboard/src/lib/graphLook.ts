import type { CSSProperties } from "react";

// How the graph looks, for the 2D and the 3D view: a calm dark surface with a
// glow at its foot and a faint grid, hairline curved edges, small soft dots.
// A node's colour is how recently its facts were seen (blue, the oldest, to
// coral, the newest), its size is how many facts it has, an edge takes the
// colour of its family. The light theme is the same drawing on a pale surface,
// not an inverted one. Text, card and accent come from the dashboard's own
// tokens (index.css), so a change of theme reaches the graph with the page.

export interface GraphTheme {
  dark: boolean;
  /** The surface the graph is drawn on. */
  surface: CSSProperties;
  text: string;
  muted: string;
  card: string;
  border: string;
  accent: string;
  /** Recency, oldest to newest. */
  stops: [string, string, string];
  /** An edge at rest: its alpha and its width. */
  edgeAlpha: number;
  edgeWidth: number;
  /** A dimmed dot. */
  dim: string;
}

/** `#rrggbb` + an alpha byte; any other colour string as it is. */
export function withAlpha(color: string, alpha: number): string {
  if (!/^#[0-9a-f]{6}$/i.test(color)) return color;
  return color + Math.round(Math.min(1, Math.max(0, alpha)) * 255).toString(16).padStart(2, "0");
}

/** `222 24% 8%` (a token of index.css) as `#rrggbb`. */
function hslToken(triple: string, fallback: string): string {
  const m = triple.match(/(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%/);
  if (!m) return fallback;
  const h = Number(m[1]);
  const s = Number(m[2]) / 100;
  const l = Number(m[3]) / 100;
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const v = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(v * 255).toString(16).padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

const grid = (stroke: string, opacity: number) =>
  `url("data:image/svg+xml,${encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' width='30' height='30'><path d='M15 12V18M12 15H18' stroke='${stroke}' stroke-width='0.7' opacity='${opacity}'/></svg>`,
  )}")`;

export function readTheme(): GraphTheme {
  const root = document.documentElement;
  const dark = root.classList.contains("dark");
  const css = getComputedStyle(root);
  const token = (name: string, fallback: string) => hslToken(css.getPropertyValue(name), fallback);
  return dark
    ? {
        dark,
        surface: {
          backgroundColor: "#070b16",
          backgroundImage: `radial-gradient(ellipse 62% 62% at 50% 100%, rgba(29,78,216,0.38), rgba(30,58,138,0.12) 55%, rgba(30,58,138,0) 100%), ${grid("#8ea3c8", 0.07)}`,
          boxShadow: "inset 0 0 110px rgba(0,0,0,0.3)",
        },
        text: token("--foreground", "#e6eaf0"),
        muted: token("--muted-foreground", "#8b97a8"),
        card: token("--card", "#161b26"),
        border: token("--border", "#283041"),
        accent: token("--primary", "#4f8cf7"),
        stops: ["#5b8def", "#a98ee0", "#f0825f"],
        edgeAlpha: 0.15,
        edgeWidth: 0.7,
        dim: "#7f8aa3",
      }
    : {
        dark,
        surface: {
          backgroundColor: "#f6f8fc",
          backgroundImage: `radial-gradient(ellipse 62% 62% at 50% 100%, rgba(59,130,246,0.16), rgba(59,130,246,0.05) 55%, rgba(59,130,246,0) 100%), ${grid("#51648a", 0.11)}`,
        },
        text: token("--foreground", "#1c1f2a"),
        muted: token("--muted-foreground", "#6b7280"),
        card: token("--card", "#ffffff"),
        border: token("--border", "#e2e5ea"),
        accent: token("--primary", "#2563eb"),
        stops: ["#2f6fe0", "#8b5fd0", "#e4572e"],
        edgeAlpha: 0.15,
        edgeWidth: 0.7,
        dim: "#9aa6bd",
      };
}

const rgb = (hex: string): [number, number, number] => [
  parseInt(hex.slice(1, 3), 16),
  parseInt(hex.slice(3, 5), 16),
  parseInt(hex.slice(5, 7), 16),
];

/** The recency colour at `t`, 0 (oldest) to 1 (newest). */
export function recencyColor(t: number, stops: GraphTheme["stops"]): string {
  const x = Math.min(1, Math.max(0, t)) * 2;
  const i = Math.min(1, Math.floor(x));
  const a = rgb(stops[i]);
  const b = rgb(stops[i + 1]);
  const f = x - i;
  return (
    "#" +
    [0, 1, 2].map((k) => Math.round(a[k] + (b[k] - a[k]) * f).toString(16).padStart(2, "0")).join("")
  );
}

/** The size of a dot for a node with `degree` facts: a few pixels for a leaf,
 *  a dozen for a hub, never the disc that hid its own neighbours. */
export function dotSize(degree: number, entity: boolean): number {
  return (entity ? 2.2 : 1.5) + Math.min(6.5, Math.sqrt(degree) * 0.75);
}

/** The control point of an edge's curve: a quarter-turn off its midpoint, a
 *  tenth of its length; every edge bends the same way round, so a fan of them
 *  from one node reads as one gesture. */
export function bend(x0: number, y0: number, x1: number, y1: number): [number, number] {
  return [(x0 + x1) / 2 + (y1 - y0) * 0.1, (y0 + y1) / 2 - (x1 - x0) * 0.1];
}

/** `2026-05-07 23:52` (UTC) as the cards write a moment. */
export function stamp(ms: number): string {
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ");
}

/** Whether this browser can draw the graph: both views draw with WebGL 2
 *  (Sigma 4 for the flat one, three.js under 3d-force-graph for the 3D
 *  one), and without it Sigma throws "WebGL 2 is not supported" while the
 *  page mounts, which emptied the whole screen (QA of 14 Nov 2026, in a
 *  browser with no GPU). Asked of a canvas the page never shows, handed in
 *  so the check runs without a browser. */
export function drawsWebGL2(doc: Pick<Document, "createElement">): boolean {
  try {
    const canvas = doc.createElement("canvas") as HTMLCanvasElement;
    if (typeof canvas.getContext !== "function") return false;
    const gl = canvas.getContext("webgl2");
    if (gl === null) return false;
    // Given back at once: a browser keeps a few WebGL contexts and drops
    // the oldest past them, and this one would sit beside the graph's.
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return true;
  } catch {
    return false;
  }
}

/** What the Graph page says where the graph would be, when it cannot. */
export const NO_WEBGL2 =
  "This browser can't draw the graph: it needs WebGL 2, which is turned off or missing here (a virtual machine, a remote desktop, or hardware acceleration switched off in the browser's settings). The same facts are listed on the Facts page.";
