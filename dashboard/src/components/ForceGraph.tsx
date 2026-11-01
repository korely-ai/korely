import { useEffect, useMemo, useRef, useState } from "react";

/**
 * Dependency-free force-directed graph for the agent-memory moat view.
 *
 * The dashboard is intentionally lean (React + Tailwind + lucide, no graph
 * lib), so this is a small hand-rolled simulation: O(n²) repulsion + link
 * springs + a gentle center pull, animated with requestAnimationFrame until
 * the layout settles. Node seeding is deterministic (label hash → golden-angle
 * spiral) so the graph doesn't jump between renders. Sized for tens-to-low-
 * hundreds of nodes, which is what one developer's facts look like.
 *
 * Once the layout settles the view closes in on the nodes (`fitView`): three
 * nodes in the 920-unit frame drew labels about 5 px tall at the README's
 * width (29 Oct 2026). A node moved by hand keeps the view where it is.
 */

export interface GNode {
  id: string; // dedup key = the entity/value label
  label: string;
  kind: "entity" | "value"; // subject side vs object/literal side
  subjectType?: string; // person / org / place / unknown
  degree: number;
}

export interface GEdge {
  id: string; // fact id
  source: string;
  target: string;
  predicate: string;
  family: string;
  invalidated: boolean; // closed (superseded or ended) at the current as_of
}

interface Pt {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

const W = 920;
const H = 620;
const CX = W / 2;
const CY = H / 2;

/** The part of the frame on screen, in its units: the SVG's viewBox. */
interface View {
  x: number;
  y: number;
  w: number;
  h: number;
}
const FULL: View = { x: 0, y: 0, w: W, h: H };
const FIT_PAD = 48; // around the outermost node or label
const MAX_ZOOM = 2; // two nodes are not drawn as big as the panel
const GLIDE_MS = 450;

/** FNV-1a → [0,1). Deterministic seed from a label. */
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

function radius(n: GNode): number {
  // entities read as the anchors; values are smaller satellites.
  const base = n.kind === "entity" ? 9 : 6;
  return base + Math.min(8, n.degree * 1.4);
}

/** The view that holds every node and its label with a margin, at the
 *  frame's proportions: never closer than MAX_ZOOM, never wider than the
 *  frame. Null with no node placed. */
function fitView(nodes: GNode[], p: Map<string, Pt>): View | null {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  nodes.forEach((n) => {
    const pt = p.get(n.id);
    if (!pt) return;
    const r = radius(n);
    // The label under the node, as wide as its characters at the drawn size.
    const half = Math.max(r, Math.min(n.label.length, 22) * (n.kind === "entity" ? 11 : 10) * 0.3);
    x0 = Math.min(x0, pt.x - half);
    x1 = Math.max(x1, pt.x + half);
    y0 = Math.min(y0, pt.y - r);
    y1 = Math.max(y1, pt.y + r + 14);
  });
  if (!Number.isFinite(x0)) return null;
  const w = Math.max(x1 - x0 + 2 * FIT_PAD, ((y1 - y0 + 2 * FIT_PAD) * W) / H, W / MAX_ZOOM);
  if (w >= W) return FULL;
  const h = (w * H) / W;
  const clamp = (v: number, hi: number) => Math.max(0, Math.min(hi, v));
  return { x: clamp((x0 + x1) / 2 - w / 2, W - w), y: clamp((y0 + y1) / 2 - h / 2, H - h), w, h };
}

export function ForceGraph({
  nodes,
  edges,
  selectedId,
  onSelect,
  familyColors,
}: {
  nodes: GNode[];
  edges: GEdge[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  familyColors: Record<string, string>;
}) {
  const pos = useRef<Map<string, Pt>>(new Map());
  const dragId = useRef<string | null>(null);
  const raf = useRef<number | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [view, setView] = useState<View>(FULL);
  const viewRef = useRef<View>(FULL); // the drag reads it between renders
  const glide = useRef<number | null>(null);
  const movedByHand = useRef(false);
  const shownIds = useRef("");
  const [, setTick] = useState(0);
  const [hoverId, setHoverId] = useState<string | null>(null);

  // Adjacency for hover/selection dimming.
  const neighbors = useMemo(() => {
    const m = new Map<string, Set<string>>();
    nodes.forEach((n) => m.set(n.id, new Set()));
    edges.forEach((e) => {
      m.get(e.source)?.add(e.target);
      m.get(e.target)?.add(e.source);
    });
    return m;
  }, [nodes, edges]);

  // Seed positions for new nodes; drop positions for nodes that disappeared.
  useEffect(() => {
    const next = new Map<string, Pt>();
    nodes.forEach((n, i) => {
      const existing = pos.current.get(n.id);
      if (existing) {
        next.set(n.id, existing);
        return;
      }
      // Golden-angle phyllotactic seeding (Vogel 1979), jittered by label hash
      // so co-added nodes don't stack and layout is stable across reloads.
      const a = i * 2.399963229 + hash01(n.id) * 6.283;
      const r = 40 + Math.sqrt(i + 1) * 26;
      next.set(n.id, { x: CX + Math.cos(a) * r, y: CY + Math.sin(a) * r, vx: 0, vy: 0 });
    });
    pos.current = next;
    // Other nodes are another graph: it is fitted again, even after a drag.
    const ids = nodes.map((n) => n.id).sort().join("\n");
    if (ids !== shownIds.current) movedByHand.current = false;
    shownIds.current = ids;
    startSim();
    // A new graph restarts the layout; nothing else should.
  }, [nodes, edges]);

  function startSim() {
    let alpha = 1;
    const step = () => {
      alpha *= 0.97;
      tickSim(alpha);
      setTick((t) => t + 1);
      if (alpha > 0.02) {
        raf.current = requestAnimationFrame(step);
      } else {
        raf.current = null;
        if (!movedByHand.current) {
          const v = fitView(nodes, pos.current);
          if (v) glideTo(v);
        }
      }
    };
    if (raf.current) cancelAnimationFrame(raf.current);
    raf.current = requestAnimationFrame(step);
  }

  // From the view on screen to `to`, eased; at once for reduced motion.
  function glideTo(to: View) {
    if (glide.current) cancelAnimationFrame(glide.current);
    const from = viewRef.current;
    const t0 = performance.now();
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const step = (now: number) => {
      const k = still ? 1 : Math.min(1, (now - t0) / GLIDE_MS);
      const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      const v = {
        x: from.x + (to.x - from.x) * e,
        y: from.y + (to.y - from.y) * e,
        w: from.w + (to.w - from.w) * e,
        h: from.h + (to.h - from.h) * e,
      };
      viewRef.current = v;
      setView(v);
      glide.current = k < 1 ? requestAnimationFrame(step) : null;
    };
    glide.current = requestAnimationFrame(step);
  }

  function tickSim(alpha: number) {
    const p = pos.current;
    const arr = nodes.map((n) => ({ n, pt: p.get(n.id)! })).filter((x) => x.pt);

    // Repulsion (capped Coulomb), degree-weighted so hubs claim space.
    for (let i = 0; i < arr.length; i++) {
      for (let j = i + 1; j < arr.length; j++) {
        const a = arr[i].pt;
        const b = arr[j].pt;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {
          dx = (hash01(arr[i].n.id) - 0.5) * 2;
          dy = (hash01(arr[j].n.id) - 0.5) * 2;
          d2 = dx * dx + dy * dy;
        }
        const d = Math.sqrt(d2);
        const rep =
          (2600 * alpha) /
          d2 *
          (1 + (arr[i].n.degree + arr[j].n.degree) * 0.05);
        const fx = (dx / d) * rep;
        const fy = (dy / d) * rep;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    // Link springs toward a rest length.
    edges.forEach((e) => {
      const a = p.get(e.source);
      const b = p.get(e.target);
      if (!a || !b) return;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const rest = 118;
      const k = (d - rest) * 0.045 * alpha;
      const fx = (dx / d) * k;
      const fy = (dy / d) * k;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    });

    // Center pull + integrate + damp. Keep nodes inside the frame.
    arr.forEach(({ n, pt }) => {
      if (dragId.current === n.id) {
        pt.vx = 0;
        pt.vy = 0;
        return;
      }
      pt.vx += (CX - pt.x) * 0.0016 * alpha;
      pt.vy += (CY - pt.y) * 0.0016 * alpha;
      pt.vx *= 0.86;
      pt.vy *= 0.86;
      pt.x += Math.max(-12, Math.min(12, pt.vx));
      pt.y += Math.max(-12, Math.min(12, pt.vy));
      const m = 24;
      pt.x = Math.max(m, Math.min(W - m, pt.x));
      pt.y = Math.max(m, Math.min(H - m, pt.y));
    });
  }

  useEffect(() => () => {
    if (raf.current) cancelAnimationFrame(raf.current);
    if (glide.current) cancelAnimationFrame(glide.current);
  }, []);

  // ── drag (pointer → svg user space) ──────────────────────────────────────
  function toUser(e: React.PointerEvent): { x: number; y: number } {
    const svg = svgRef.current!;
    const r = svg.getBoundingClientRect();
    const v = viewRef.current;
    return {
      x: v.x + ((e.clientX - r.left) / r.width) * v.w,
      y: v.y + ((e.clientY - r.top) / r.height) * v.h,
    };
  }

  function onPointerDownNode(e: React.PointerEvent, id: string) {
    e.stopPropagation();
    (e.target as Element).setPointerCapture(e.pointerId);
    dragId.current = id;
    onSelect(id);
  }

  function onPointerMove(e: React.PointerEvent) {
    if (!dragId.current) return;
    const u = toUser(e);
    const pt = pos.current.get(dragId.current);
    if (pt) {
      movedByHand.current = true;
      if (glide.current) cancelAnimationFrame(glide.current);
      glide.current = null;
      pt.x = u.x;
      pt.y = u.y;
      pt.vx = 0;
      pt.vy = 0;
      setTick((t) => t + 1);
    }
  }

  function onPointerUp() {
    if (dragId.current) {
      dragId.current = null;
      startSim(); // re-heat so the graph re-settles around the moved node
    }
  }

  const focus = hoverId || selectedId;
  const focusSet = focus ? new Set([focus, ...(neighbors.get(focus) || [])]) : null;

  return (
    <svg
      ref={svgRef}
      viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
      className="h-full w-full touch-none select-none"
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
      onClick={() => onSelect(null)}
    >
      {/* edges */}
      {edges.map((e) => {
        const a = pos.current.get(e.source);
        const b = pos.current.get(e.target);
        if (!a || !b) return null;
        const dim = focusSet && !(focusSet.has(e.source) && focusSet.has(e.target));
        const color = familyColors[e.family] || familyColors.other;
        const showLabel = focus && (e.source === focus || e.target === focus);
        const mx = (a.x + b.x) / 2;
        const my = (a.y + b.y) / 2;
        return (
          <g key={e.id} style={{ opacity: dim ? 0.12 : 1 }}>
            <line
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={e.invalidated ? "currentColor" : color}
              strokeWidth={e.invalidated ? 1 : 1.6}
              strokeDasharray={e.invalidated ? "4 4" : undefined}
              className={e.invalidated ? "text-muted-foreground/50" : undefined}
            />
            {showLabel && (
              <text
                x={mx}
                y={my - 3}
                textAnchor="middle"
                className="pointer-events-none fill-muted-foreground"
                style={{ fontSize: 10, fontFamily: "ui-monospace, monospace" }}
              >
                {e.predicate}
                {e.invalidated ? " ⊘" : ""}
              </text>
            )}
          </g>
        );
      })}

      {/* nodes */}
      {nodes.map((n) => {
        const pt = pos.current.get(n.id);
        if (!pt) return null;
        const dim = focusSet && !focusSet.has(n.id);
        const r = radius(n);
        const isEntity = n.kind === "entity";
        const sel = n.id === selectedId;
        // Entities pick up the warm anchor tone; values stay neutral.
        const fill = isEntity ? "hsl(var(--primary))" : "hsl(var(--card))";
        const stroke = isEntity ? "hsl(var(--primary))" : "hsl(var(--muted-foreground))";
        return (
          <g
            key={n.id}
            style={{ opacity: dim ? 0.18 : 1, cursor: "pointer" }}
            onPointerDown={(e) => onPointerDownNode(e, n.id)}
            onPointerEnter={() => setHoverId(n.id)}
            onPointerLeave={() => setHoverId(null)}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(n.id);
            }}
          >
            {sel && (
              <circle cx={pt.x} cy={pt.y} r={r + 5} fill="none" stroke="hsl(var(--ring))" strokeWidth={1.5} />
            )}
            <circle
              cx={pt.x}
              cy={pt.y}
              r={r}
              fill={fill}
              fillOpacity={isEntity ? 0.9 : 1}
              stroke={stroke}
              strokeWidth={isEntity ? 0 : 1.4}
            />
            <text
              x={pt.x}
              y={pt.y + r + 11}
              textAnchor="middle"
              className="pointer-events-none fill-foreground"
              style={{ fontSize: isEntity ? 11 : 10, fontWeight: isEntity ? 600 : 400 }}
            >
              {n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
