import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import ForceGraph3D from "3d-force-graph";
import type { GNode, GEdge } from "@/lib/graphTypes";
import { adjacency, focusOn, short, type GraphHandle } from "@/lib/graphFocus";
import { readTheme, recencyColor } from "@/lib/graphLook";

/**
 * The same graph as FactGraph, in three dimensions (3d-force-graph, on three.js
 * and d3-force-3d). It is a view for looking around, not for reading a
 * thousand labels: names appear on hover, the selected node lights its
 * strongest neighbours (the same bounded neighbourhood as the 2D view) and the
 * rest fades, or is not drawn in the focus mode. It is loaded only when the
 * person opens the 3D tab, because three.js is most of the weight.
 */

const FALLBACK = "#8b97a8";

export const FactGraph3D = forwardRef<
  GraphHandle,
  {
    nodes: GNode[];
    edges: GEdge[];
    selectedId: string | null;
    onSelect: (id: string | null) => void;
    familyColors: Record<string, string>;
    focusOnly: boolean;
  }
>(function FactGraph3D({ nodes, edges, selectedId, onSelect, familyColors, focusOnly }, ref) {
  const box = useRef<HTMLDivElement | null>(null);
  const selected = useRef<string | null>(selectedId);
  const isolate = useRef(focusOnly);
  const pick = useRef(onSelect);
  pick.current = onSelect;
  const graph = useRef<InstanceType<typeof ForceGraph3D> | null>(null);
  const relight = useRef<() => void>(() => {});
  const [theme, setTheme] = useState(readTheme);

  // The theme follows the page: a `dark` class on <html>.
  useEffect(() => {
    const watch = new MutationObserver(() => setTheme(readTheme()));
    watch.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => watch.disconnect();
  }, []);

  const dolly = (factor: number) => {
    const g = graph.current;
    if (!g) return;
    const c = g.cameraPosition();
    g.cameraPosition({ x: c.x * factor, y: c.y * factor, z: c.z * factor }, undefined, 250);
  };
  useImperativeHandle(
    ref,
    () => ({
      zoomIn: () => dolly(0.7),
      zoomOut: () => dolly(1.4),
      reset: () => graph.current?.zoomToFit(500),
      toggleLayout: () => {},
    }),
    [],
  );

  useEffect(() => {
    selected.current = selectedId;
    isolate.current = focusOnly;
    // Same accessors again: three-forcegraph repaints when they are set.
    const g = graph.current;
    if (!g) return;
    relight.current();
    g.nodeColor(g.nodeColor());
    g.linkColor(g.linkColor());
    g.nodeVisibility(g.nodeVisibility());
    g.linkVisibility(g.linkVisibility());
  }, [selectedId, focusOnly]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ids = new Set(nodes.map((n) => n.id));
    const links = edges
      .filter((e) => ids.has(e.source) && ids.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, family: e.family, invalidated: e.invalidated }));
    const adj = adjacency(edges);
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const degree = (id: string) => byId.get(id)?.degree ?? 0;
    let lo = Infinity;
    let hi = -Infinity;
    nodes.forEach((n) => {
      if (n.lastSeen === undefined) return;
      lo = Math.min(lo, n.lastSeen);
      hi = Math.max(hi, n.lastSeen);
    });
    const colorOf = (n: GNode): string =>
      n.lastSeen !== undefined && hi > lo ? recencyColor((n.lastSeen - lo) / (hi - lo), theme.stops) : recencyColor(0.5, theme.stops);
    // The bright part of the picture: the selected node and its strongest neighbours.
    const lit = (): Set<string> | null => {
      const s = selected.current;
      return s ? new Set([s, ...focusOn(adj, degree, s, false).lit]) : null;
    };
    let on = lit();
    relight.current = () => (on = lit());
    const endId = (v: { id: string } | string) => (typeof v === "object" ? v.id : v);
    const text = getComputedStyle(el).color;
    let fitted = false;
    const g = new ForceGraph3D(el)
      .backgroundColor("rgba(0,0,0,0)")
      .width(el.clientWidth)
      .height(el.clientHeight)
      .graphData({ nodes: nodes.map((n) => ({ ...n })), links })
      .nodeId("id")
      .nodeLabel((n) => `<span style="color:${text}">${String(short((n as GNode).label, 80)).replace(/</g, "&lt;")}</span>`)
      .nodeVal((n) => 1 + Math.min(16, (n as GNode).degree))
      .nodeColor((n) => {
        return on && !on.has((n as GNode).id) ? theme.dim : colorOf(n as GNode);
      })
      .nodeVisibility((n) => !(isolate.current && on && !on.has((n as GNode).id)))
      .nodeOpacity(0.9)
      .linkColor((l) => {
        const k = l as { family: string; source: { id: string } | string; target: { id: string } | string };
        const a = endId(k.source);
        const b = endId(k.target);
        const s = selected.current;
        // Only the edges of the selected node stay bright; its strongest neighbours' own edges do not.
        return on && !(s && (a === s || b === s) && on.has(a) && on.has(b)) ? theme.dim : familyColors[k.family] ?? FALLBACK;
      })
      .linkVisibility((l) => {
        const k = l as { source: { id: string } | string; target: { id: string } | string };
        return !(isolate.current && on && !(on.has(endId(k.source)) && on.has(endId(k.target))));
      })
      .linkOpacity(0.3)
      .linkWidth((l) => ((l as { invalidated: boolean }).invalidated ? 0 : 0.5))
      .onEngineStop(() => {
        // Once, when the first layout has settled: the whole graph in view.
        if (fitted) return;
        fitted = true;
        g.zoomToFit(400);
      })
      .onNodeClick((n) => pick.current((n as GNode).id))
      .onBackgroundClick(() => pick.current(null));
    graph.current = g;
    const ro = new ResizeObserver(() => g.width(el.clientWidth).height(el.clientHeight));
    ro.observe(el);
    return () => {
      ro.disconnect();
      g.pauseAnimation();
      g._destructor();
      graph.current = null;
    };
  }, [nodes, edges, familyColors, theme]);

  return (
    <div
      ref={box}
      role="img"
      aria-label={`3D graph of ${edges.length} facts between ${nodes.length} nodes. Drag to turn, scroll to zoom, click a node to select it.`}
      className="h-full w-full text-foreground"
      style={theme.surface}
    />
  );
});

export default FactGraph3D;
