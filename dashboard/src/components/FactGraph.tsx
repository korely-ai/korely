import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import Sigma from "sigma";
import type { NodeDisplayData } from "sigma/types";
import type { GNode, GEdge } from "@/lib/graphTypes";
import {
  LABELLED_NEIGHBOURS,
  adjacency,
  focusOn,
  short,
  strongest,
  type Focus,
  type GraphHandle,
} from "@/lib/graphFocus";
import { bend, dotSize, readTheme, recencyColor, stamp, withAlpha, type GraphTheme } from "@/lib/graphLook";

/**
 * The facts of the account as a graph, drawn with WebGL (Sigma.js 4, on
 * graphology), so that a thousand facts stay readable where the hand-rolled
 * SVG simulation this replaces stopped being so at a few hundred (screenshot
 * of 1 Nov 2026: 959 nodes, every label drawn, one grey ball).
 *
 * What makes it readable, and none of it is the library alone:
 *  - the layout is ForceAtlas2 (Jacomy et al., PLoS ONE 2014), computed once
 *    when the graph changes, with Barnes-Hut above a few hundred nodes, then
 *    left to settle for a moment in a worker (the toolbar pauses it);
 *  - a dot is coloured by how recently its facts were seen and sized by how
 *    many it has; the edges are hairline curves, drawn on a canvas under the
 *    dots (Sigma's edge programs are straight), coloured by family;
 *  - at rest a label is drawn only for the dots big enough to carry one, more
 *    as the view closes in (labelDensity, labelRenderedSizeThreshold);
 *  - a node under the pointer, or selected, is the focus: at most
 *    LABELLED_NEIGHBOURS of its neighbours are named, the strongest, and only
 *    where their labels do not overlap; its edges light up and the rest of the
 *    graph fades. In the focus mode the rest is not drawn at all.
 */

const FALLBACK = "#8b97a8";
const SETTLE_MS = 3500;

/** What the reducers and the overlay read between renders. */
interface Shown {
  focus: Focus;
  /** Only the focused node's neighbourhood is drawn. */
  isolate: boolean;
  /** The neighbours that are named, and on which side of the dot. */
  labelled: Map<string, "left" | "right">;
  /** The focus, the neighbours drawn bright and the ones named. */
  bright: Set<string>;
}

export const FactGraph = forwardRef<
  GraphHandle,
  {
    nodes: GNode[];
    edges: GEdge[];
    selectedId: string | null;
    onSelect: (id: string | null) => void;
    familyColors: Record<string, string>;
    /** Draw only the selected node and its strongest neighbours. */
    focusOnly: boolean;
    onLeaveFocus: () => void;
    /** True while the layout is still moving. */
    onLayoutRunning: (running: boolean) => void;
  }
>(function FactGraph({ nodes, edges, selectedId, onSelect, familyColors, focusOnly, onLeaveFocus, onLayoutRunning }, ref) {
  const box = useRef<HTMLDivElement | null>(null);
  const overlay = useRef<HTMLCanvasElement | null>(null);
  const sigma = useRef<Sigma | null>(null);
  const toggle = useRef<() => void>(() => {});
  const refocus = useRef<() => void>(() => {});
  const [theme, setTheme] = useState<GraphTheme>(readTheme);
  const [expanded, setExpanded] = useState(false);

  // Read by the reducers, the overlay and the click handler between renders.
  const themeRef = useRef(theme);
  themeRef.current = theme;
  const hovered = useRef<string | null>(null);
  const selected = useRef<string | null>(selectedId);
  const mode = useRef({ focusOnly, expanded });
  const shown = useRef<Shown | null>(null);
  const pick = useRef(onSelect);
  pick.current = onSelect;
  const layoutState = useRef(onLayoutRunning);
  layoutState.current = onLayoutRunning;

  const data = useMemo(() => {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const dominantFamily = new Map<string, Map<string, number>>();
    edges.forEach((e) =>
      [e.source, e.target].forEach((id) => {
        const m = dominantFamily.get(id) ?? new Map<string, number>();
        m.set(e.family, (m.get(e.family) ?? 0) + 1);
        dominantFamily.set(id, m);
      }),
    );
    let lo = Infinity;
    let hi = -Infinity;
    nodes.forEach((n) => {
      if (n.lastSeen === undefined) return;
      lo = Math.min(lo, n.lastSeen);
      hi = Math.max(hi, n.lastSeen);
    });
    return {
      adj: adjacency(edges),
      byId,
      degree: (id: string) => byId.get(id)?.degree ?? 0,
      family: (id: string): string => {
        let best = "other";
        let top = -1;
        dominantFamily.get(id)?.forEach((c, fam) => {
          if (c > top) {
            top = c;
            best = fam;
          }
        });
        return best;
      },
      lo,
      hi,
    };
  }, [nodes, edges]);
  const dataRef = useRef(data);
  dataRef.current = data;

  // The theme follows the page: a `dark` class on <html>, toggled by the dashboard.
  useEffect(() => {
    const watch = new MutationObserver(() => setTheme(readTheme()));
    watch.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => watch.disconnect();
  }, []);

  useEffect(() => {
    setExpanded(false);
  }, [selectedId]);

  useImperativeHandle(
    ref,
    () => ({
      zoomIn: () => void sigma.current?.getCamera().zoomIn({ duration: 250 }),
      zoomOut: () => void sigma.current?.getCamera().zoomOut({ duration: 250 }),
      reset: () => void sigma.current?.getCamera().reset({ duration: 450 }),
      toggleLayout: () => toggle.current(),
    }),
    [],
  );

  useEffect(() => {
    const el = box.current;
    const canvas = overlay.current;
    if (!el || !canvas) return;
    const calm = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const g = new Graph({ multi: false, type: "undirected" });
    nodes.forEach((n, i) => {
      // Golden-angle seeding (Vogel 1979): the same facts start from the same
      // places, and ForceAtlas2 moves them from there.
      const a = i * 2.399963229;
      const r = 6 * Math.sqrt(i + 1);
      const span = data.hi - data.lo;
      g.addNode(n.id, {
        label: short(n.label),
        x: Math.cos(a) * r,
        y: Math.sin(a) * r,
        size: dotSize(n.degree, n.kind === "entity"),
        // 0 oldest .. 1 newest; a graph with one moment is all mid-way.
        t: n.lastSeen !== undefined && span > 0 ? (n.lastSeen - data.lo) / span : 0.5,
      });
    });
    edges.forEach((e) => {
      if (!g.hasNode(e.source) || !g.hasNode(e.target) || g.hasEdge(e.source, e.target)) return;
      g.addEdge(e.source, e.target, { family: e.family, invalidated: e.invalidated });
    });
    // Log-linear attraction (Noack 2007, in ForceAtlas2) pulls a hub's leaves
    // into one tight cluster and pushes the clusters apart, where the plain
    // model leaves one dense blob with long spokes.
    const settings = {
      ...forceAtlas2.inferSettings(g),
      barnesHutOptimize: g.order > 400,
      linLogMode: true,
      gravity: 0.08,
      scalingRatio: 12,
      slowDown: 2,
    };
    if (g.order > 1) forceAtlas2.assign(g, { iterations: 250, settings });

    const s = new Sigma(g, el, {
      settings: {
        renderEdgeLabels: false,
        itemSizesReference: "screen",
        labelDensity: 0.5,
        labelGridCellSize: 150,
        // A small graph can name every node; a large one only the big ones.
        labelRenderedSizeThreshold: g.order <= 80 ? 0 : 5,
        minCameraRatio: 0.03,
        maxCameraRatio: 4,
      },
      styles: {
        nodes: {
          color: { attribute: "color", defaultValue: FALLBACK },
          size: { attribute: "size" },
          label: { attribute: "label" },
          labelSize: 12,
        },
      },
      nodeReducer: (n, d, attrs) => {
        const th = themeRef.current;
        const sh = shown.current;
        const base = recencyColor(Number(attrs.t), th.stops);
        const surface = String(th.surface.backgroundColor);
        const out: Partial<NodeDisplayData> = {
          color: base,
          labelColor: withAlpha(th.muted, 0.95),
          labelBackgroundColor: withAlpha(surface, 0.55),
          labelBackgroundPadding: 2,
          backdropVisibility: "hidden",
        };
        if (!sh) return out;
        if (n === sh.focus.id) {
          out.size = d.size * 1.25;
          out.zIndex = 4;
          out.highlighted = true;
          out.labelVisibility = "visible";
          out.labelColor = th.text;
          out.labelSize = 13;
          out.backdropVisibility = "visible";
          out.backdropColor = th.card;
          out.backdropBorderColor = th.accent;
          out.backdropBorderWidth = 1.5;
          out.backdropCornerRadius = 8;
          out.backdropPadding = 5;
          out.backdropShadowColor = th.dark ? "rgba(0,0,0,0.5)" : "rgba(15,23,42,0.18)";
          out.backdropShadowBlur = 14;
          out.backdropArea = "both";
        } else if (sh.bright.has(n)) {
          out.zIndex = 3;
          out.labelVisibility = sh.labelled.has(n) ? "visible" : "hidden";
          out.labelPosition = sh.labelled.get(n);
          out.labelColor = withAlpha(th.text, 0.85);
        } else if (sh.isolate) {
          out.visibility = "hidden";
          out.labelVisibility = "hidden";
        } else {
          const near = sh.focus.ranked.length > 0 && data.adj.get(sh.focus.id)?.has(n);
          out.color = withAlpha(near ? base : th.dim, near ? 0.5 : 0.14);
          out.labelVisibility = "hidden";
          out.zIndex = near ? 1 : 0;
        }
        return out;
      },
      // The edges are drawn on the canvas under the dots (see `paint`).
      edgeReducer: () => ({ visibility: "hidden" }),
    });
    sigma.current = s;

    // The focus: the node under the pointer, else the selected one; in the
    // focus mode only the selected one, so that the view does not jump about
    // under the pointer. Which neighbours are named is decided on the screen,
    // strongest first, skipping a label that would sit on one already placed.
    const focusNow = () => {
      const sel = selected.current;
      const isolate = mode.current.focusOnly && !!sel;
      const id = isolate ? sel : hovered.current ?? sel;
      if (!id || !g.hasNode(id)) {
        shown.current = null;
        return;
      }
      const focus = focusOn(data.adj, data.degree, id, isolate && mode.current.expanded);
      const { width, height } = s.getDimensions();
      const taken: number[][] = [];
      const room = (nid: string): "left" | "right" | null => {
        const v = s.graphToViewport(g.getNodeAttributes(nid) as { x: number; y: number });
        const w = String(g.getNodeAttribute(nid, "label")).length * 6.6 + 18;
        for (const side of ["right", "left"] as const) {
          const r = side === "right" ? [v.x - 6, v.y - 11, v.x + w, v.y + 11] : [v.x - w, v.y - 11, v.x + 6, v.y + 11];
          if (r[0] < 0 || r[1] < 0 || r[2] > width || r[3] > height) continue;
          if (taken.some((t) => r[0] < t[2] && r[2] > t[0] && r[1] < t[3] && r[3] > t[1])) continue;
          taken.push(r);
          return side;
        }
        return null;
      };
      room(id);
      const labelled = new Map<string, "left" | "right">();
      for (const nid of focus.ranked.slice(0, 4 * LABELLED_NEIGHBOURS)) {
        if (labelled.size >= LABELLED_NEIGHBOURS) break;
        const side = room(nid);
        if (side) labelled.set(nid, side);
      }
      shown.current = { focus, isolate, labelled, bright: new Set([id, ...focus.lit, ...labelled.keys()]) };
    };

    // The hairline curves, the halos and the midpoint dots, under the dots.
    const paint = () => {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const { width, height } = s.getDimensions();
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const th = themeRef.current;
      const sh = shown.current;
      const at = new Map<string, { x: number; y: number }>();
      const where = (id: string) => {
        let p = at.get(id);
        if (!p) at.set(id, (p = s.graphToViewport(g.getNodeAttributes(id) as { x: number; y: number })));
        return p;
      };
      const lit: [number, number, number, number, string][] = [];
      ctx.lineWidth = th.edgeWidth;
      g.forEachEdge((_e, a, src, tgt) => {
        const color = familyColors[a.family as string] ?? FALLBACK;
        let alpha = th.edgeAlpha * (a.invalidated ? 0.45 : 1);
        if (sh) {
          const f = sh.focus.id;
          const touches = src === f || tgt === f;
          if (sh.isolate && !touches) return;
          if (touches && sh.bright.has(src === f ? tgt : src)) {
            const p = where(src);
            const q = where(tgt);
            lit.push([p.x, p.y, q.x, q.y, color]);
            return;
          }
          if (sh.isolate) return;
          alpha *= touches ? 0.5 : 0.12;
        }
        const p = where(src);
        const q = where(tgt);
        if ((p.x < -20 && q.x < -20) || (p.x > width + 20 && q.x > width + 20)) return;
        const [cx, cy] = bend(p.x, p.y, q.x, q.y);
        ctx.strokeStyle = withAlpha(color, alpha);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.quadraticCurveTo(cx, cy, q.x, q.y);
        ctx.stroke();
      });
      // A soft halo under each dot that is not faded.
      g.forEachNode((n, a) => {
        if (sh && !sh.bright.has(n)) return;
        const p = where(n);
        if (p.x < -30 || p.y < -30 || p.x > width + 30 || p.y > height + 30) return;
        const r = s.scaleSize(Number(a.size)) * 2.2;
        ctx.fillStyle = withAlpha(recencyColor(Number(a.t), th.stops), th.dark ? 0.12 : 0.16);
        ctx.beginPath();
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fill();
      });
      // The edges of the focus, bright, each with a dot at its midpoint.
      lit.forEach(([x0, y0, x1, y1, color]) => {
        const [cx, cy] = bend(x0, y0, x1, y1);
        ctx.strokeStyle = withAlpha(color, 0.8);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.quadraticCurveTo(cx, cy, x1, y1);
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(0.25 * x0 + 0.5 * cx + 0.25 * x1, 0.25 * y0 + 0.5 * cy + 0.25 * y1, 2.2, 0, Math.PI * 2);
        ctx.fill();
      });
    };
    s.on("afterRender", paint);
    focusNow();

    const redo = () => {
      focusNow();
      s.refresh();
    };
    refocus.current = redo;
    s.on("enterNode", ({ node }) => {
      if (mode.current.focusOnly && selected.current) return;
      hovered.current = node;
      redo();
    });
    s.on("leaveNode", () => {
      if (hovered.current === null) return;
      hovered.current = null;
      redo();
    });
    s.on("clickNode", ({ node }) => pick.current(node));
    s.on("clickStage", () => pick.current(null));
    // The labels are placed for the view as it is: again when it stops moving.
    let settle = 0;
    s.getCamera().on("updated", () => {
      if (!shown.current) return;
      window.clearTimeout(settle);
      settle = window.setTimeout(redo, 140);
    });
    const size = new ResizeObserver(() => {
      s.resize();
      s.refresh();
    });
    size.observe(el);

    // Let the layout settle for a moment, and let the toolbar stop it.
    let layout: FA2Layout | null = null;
    let timer = 0;
    const stop = () => {
      window.clearTimeout(timer);
      layout?.stop();
      layoutState.current(false);
    };
    const start = () => {
      if (!layout) return;
      layout.start();
      layoutState.current(true);
      timer = window.setTimeout(stop, SETTLE_MS);
    };
    toggle.current = () => (layout?.isRunning() ? stop() : start());
    if (g.order > 1 && !calm) {
      layout = new FA2Layout(g, { settings: { ...settings, slowDown: 8 } });
      start();
    } else {
      layoutState.current(false);
    }

    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(settle);
      size.disconnect();
      layout?.kill();
      s.kill();
      sigma.current = null;
      shown.current = null;
      hovered.current = null;
      toggle.current = () => {};
      refocus.current = () => {};
    };
  }, [data, nodes, edges, familyColors]);

  // The selection, the mode or the theme changed: draw the focus again, and
  // bring the neighbourhood into view.
  useEffect(() => {
    const s = sigma.current;
    selected.current = selectedId;
    mode.current = { focusOnly, expanded };
    if (!s) return;
    const g = s.getGraph();
    const sel = selectedId && g.hasNode(selectedId) ? selectedId : null;
    refocus.current();
    if (!sel) return;
    const calm = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const duration = calm ? 0 : 550;
    const cam = s.getCamera();
    const frame = (id: string) => s.graphToFramedGraph(g.getNodeAttributes(id) as { x: number; y: number });
    if (focusOnly) {
      const ids = [sel, ...strongest(dataRef.current.adj, dataRef.current.degree, sel).slice(0, expanded ? 60 : LABELLED_NEIGHBOURS)];
      const pts = ids.map(frame);
      const xs = pts.map((p) => p.x);
      const ys = pts.map((p) => p.y);
      const bw = Math.max(...xs) - Math.min(...xs);
      const bh = Math.max(...ys) - Math.min(...ys);
      void cam.animate(
        {
          x: (Math.max(...xs) + Math.min(...xs)) / 2,
          y: (Math.max(...ys) + Math.min(...ys)) / 2,
          ratio: Math.min(1, Math.max(0.05, Math.max(bw, bh) * 1.5 + 0.04)),
        },
        { duration },
      );
    } else {
      const v = s.graphToViewport(g.getNodeAttributes(sel) as { x: number; y: number });
      const { width, height } = s.getDimensions();
      if (v.x < width * 0.15 || v.x > width * 0.85 || v.y < height * 0.15 || v.y > height * 0.85) {
        void cam.animate(frame(sel), { duration });
      }
    }
  }, [selectedId, focusOnly, expanded, theme]);

  const sel = selectedId ? data.byId.get(selectedId) : undefined;
  const ranked = useMemo(
    () => (selectedId ? strongest(data.adj, data.degree, selectedId) : []),
    [selectedId, data],
  );
  const fam = sel ? data.family(sel.id) : "other";
  const dates =
    Number.isFinite(data.lo) && data.hi > data.lo ? [data.lo, data.hi].map((ms) => new Date(ms).toISOString().slice(0, 10)) : null;

  return (
    <div className="relative h-full w-full overflow-hidden" style={theme.surface}>
      <canvas ref={overlay} aria-hidden="true" className="pointer-events-none absolute inset-0 h-full w-full" />
      <div
        ref={box}
        role="img"
        aria-label={`Graph of ${edges.length} facts between ${nodes.length} nodes. Hover or click a node to see its neighbours.`}
        className="absolute inset-0 text-foreground"
      />
      {/* what colour and size say */}
      <div className="pointer-events-none absolute left-3 top-3 hidden select-none text-[10px] uppercase tracking-wider text-muted-foreground sm:block">
        {dates && (
          <div className="mb-3">
            <p className="mb-1 font-medium">Recency · mentioned</p>
            <div
              className="h-1.5 w-36 rounded-full"
              style={{ background: `linear-gradient(90deg, ${theme.stops.join(", ")})` }}
            />
            <p className="mt-1 flex w-36 justify-between font-mono normal-case tracking-normal">
              <span>{dates[0]}</span>
              <span>{dates[1]}</span>
            </p>
          </div>
        )}
        <p className="mb-1 font-medium">Facts per node</p>
        <p className="flex items-center gap-1.5 font-mono normal-case tracking-normal">
          few
          {[3, 5, 8].map((d) => (
            <span key={d} className="inline-block rounded-full bg-muted-foreground/70" style={{ width: d, height: d }} />
          ))}
          many
        </p>
      </div>

      {/* the focus mode says how much it shows, and how to widen or leave it */}
      {focusOnly && sel && (
        <div className="absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-border bg-card/90 px-3 py-1.5 text-xs text-muted-foreground backdrop-blur">
          <span>
            {Math.min(ranked.length, expanded ? 60 : LABELLED_NEIGHBOURS)} of {ranked.length} links
          </span>
          {ranked.length > LABELLED_NEIGHBOURS && (
            <button type="button" onClick={() => setExpanded((x) => !x)} className="text-foreground underline-offset-2 hover:underline">
              {expanded ? "Show fewer" : `+${Math.min(ranked.length, 60) - LABELLED_NEIGHBOURS} more`}
            </button>
          )}
          <button type="button" onClick={onLeaveFocus} className="text-foreground underline-offset-2 hover:underline">
            Leave focus
          </button>
        </div>
      )}

      {/* the selected node, on a card */}
      {sel && (
        <div className="pointer-events-none absolute bottom-3 right-3 hidden w-72 rounded-xl border border-border bg-card/95 p-3.5 text-card-foreground shadow-lg backdrop-blur sm:block">
          <p className="flex items-center gap-2.5 text-xs">
            <span className="font-semibold uppercase tracking-wide" style={{ color: familyColors[fam] ?? FALLBACK }}>
              {fam}
            </span>
            <span className="text-muted-foreground">
              {ranked.length} {ranked.length === 1 ? "link" : "links"}
            </span>
          </p>
          <p className="mt-2 break-words text-sm leading-snug">{short(sel.label, 110)}</p>
          <div className="my-2.5 border-t border-border" />
          {sel.lastSeen !== undefined && (
            <p className="flex items-baseline justify-between text-xs">
              <span className="uppercase tracking-wide text-muted-foreground">Mentioned</span>
              <span className="font-mono tabular-nums">{stamp(sel.lastSeen)}</span>
            </p>
          )}
          {ranked.length > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {ranked.slice(0, 3).map((id) => (
                <span key={id} className="rounded-md bg-secondary px-2 py-0.5 text-xs">
                  {short(id, 22)}
                </span>
              ))}
            </div>
          )}
          <p className="mt-2 font-mono text-[10px] text-muted-foreground">
            {sel.kind}
            {sel.subjectType && sel.subjectType !== "unknown" ? ` · ${sel.subjectType}` : ""}
          </p>
        </div>
      )}
    </div>
  );
});
