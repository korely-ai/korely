import type { GNode } from "@/lib/graphTypes";
import { short } from "@/lib/graphFocus";
import { stamp } from "@/lib/graphLook";

/** What a node is, as the server typed it: the subject's type when the fact
 *  said one (person, org, place), otherwise which side of the facts it sits
 *  on (an entity, or a value). */
export function nodeType(node: Pick<GNode, "kind" | "subjectType">): string {
  return node.subjectType && node.subjectType !== "unknown" ? node.subjectType : node.kind;
}

/**
 * The card over the graph for the selected node. Its first word says what
 * the node is; the colour beside it says the family of most of its facts,
 * named as that.
 *
 * Up to 1.0.39 the first word was the family of most of the node's facts,
 * set where a reader looks for what the node is: a customer whose facts were
 * written by hand, with relations of the family `other`, read "OTHER" (seen
 * on 17 Nov 2026), as if the person were classed as other.
 *
 * Its own module, not FactGraph's: that one loads sigma, which reads
 * WebGL2RenderingContext as it loads, so node draws this one
 * (components/NodeCard.behaviour.tsx).
 */
export function NodeCard({
  node,
  family,
  familyColor,
  links,
  neighbours,
}: {
  node: GNode;
  /** The family most of the node's facts belong to. */
  family: string;
  familyColor: string;
  /** How many other nodes the node is linked to. */
  links: number;
  /** The strongest of them, first. */
  neighbours: string[];
}) {
  return (
    <div className="pointer-events-none absolute bottom-3 right-3 hidden w-72 rounded-xl border border-border bg-card/95 p-3.5 text-card-foreground shadow-lg backdrop-blur sm:block">
      <p className="flex items-center gap-2.5 text-xs">
        <span className="font-semibold uppercase tracking-wide">{nodeType(node)}</span>
        <span className="text-muted-foreground">
          {links} {links === 1 ? "link" : "links"}
        </span>
      </p>
      <p className="mt-2 break-words text-sm leading-snug">{short(node.label, 110)}</p>
      <div className="my-2.5 border-t border-border" />
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span aria-hidden="true" className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: familyColor }} />
        <span>
          Mostly <span className="text-foreground">{family}</span> facts
        </span>
      </p>
      {node.lastSeen !== undefined && (
        <p className="mt-1.5 flex items-baseline justify-between text-xs">
          <span className="uppercase tracking-wide text-muted-foreground">Mentioned</span>
          <span className="font-mono tabular-nums">{stamp(node.lastSeen)}</span>
        </p>
      )}
      {neighbours.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {neighbours.slice(0, 3).map((id) => (
            <span key={id} className="rounded-md bg-secondary px-2 py-0.5 text-xs">
              {short(id, 22)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
