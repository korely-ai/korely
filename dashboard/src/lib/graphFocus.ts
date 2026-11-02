import type { GEdge } from "@/lib/graphTypes";

// What the graph is allowed to say about one node at a time. The first drawing
// that lit "every neighbour of the focused node" turned a hub with four
// hundred facts into a wall of labels (screenshot of 2 Nov 2026), so the
// neighbourhood is bounded here, once, for the 2D and the 3D view.

/** How many neighbours of the focused node may carry a label. */
export const LABELLED_NEIGHBOURS = 12;

/** How many neighbours "show more" lights in a focus: dots, hover for names. */
export const EXPANDED_NEIGHBOURS = 60;

/** For each node, its neighbours and how many facts join the pair. */
export type Adjacency = Map<string, Map<string, number>>;

export function adjacency(edges: GEdge[]): Adjacency {
  const adj: Adjacency = new Map();
  const bump = (a: string, b: string) => {
    let row = adj.get(a);
    if (!row) adj.set(a, (row = new Map()));
    row.set(b, (row.get(b) ?? 0) + 1);
  };
  edges.forEach((e) => {
    if (e.source === e.target) return;
    bump(e.source, e.target);
    bump(e.target, e.source);
  });
  return adj;
}

/** The neighbours of `id`, the strongest first: the most facts between the
 *  pair, then the best connected, then the name, so the order is the same on
 *  every render. */
export function strongest(adj: Adjacency, degree: (id: string) => number, id: string): string[] {
  const row = adj.get(id);
  if (!row) return [];
  return [...row.keys()].sort(
    (a, b) => (row.get(b) ?? 0) - (row.get(a) ?? 0) || degree(b) - degree(a) || a.localeCompare(b),
  );
}

/** One node's neighbourhood as the screen shows it. */
export interface Focus {
  id: string;
  /** Every neighbour, strongest first. */
  ranked: string[];
  /** The neighbours drawn bright, with their edges. */
  lit: Set<string>;
}

export function focusOn(
  adj: Adjacency,
  degree: (id: string) => number,
  id: string,
  expanded: boolean,
): Focus {
  const ranked = strongest(adj, degree, id);
  return { id, ranked, lit: new Set(ranked.slice(0, expanded ? EXPANDED_NEIGHBOURS : LABELLED_NEIGHBOURS)) };
}

/** What a view lets the toolbar do; the 2D and the 3D view both answer it. */
export interface GraphHandle {
  zoomIn(): void;
  zoomOut(): void;
  reset(): void;
  /** Pause or resume the layout; a view with none ignores it. */
  toggleLayout(): void;
}

/** A label as long as the line it sits on can bear. */
export function short(label: string, max = 34): string {
  return label.length > max ? label.slice(0, max - 1).trimEnd() + "…" : label;
}
