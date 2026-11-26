// What GraphPage hands the graph: a node is an entity or a value, an edge is a fact.
export interface GNode {
  id: string; // dedup key = the entity/value label
  label: string;
  kind: "entity" | "value"; // subject side vs object/literal side
  subjectType?: string; // person / org / place / unknown
  degree: number;
  lastSeen?: number; // ms of the newest fact that touches it (its colour on the graph)
}

export interface GEdge {
  id: string; // fact id
  source: string;
  target: string;
  predicate: string;
  family: string;
  invalidated: boolean; // closed (superseded or ended) at the current as_of
}
