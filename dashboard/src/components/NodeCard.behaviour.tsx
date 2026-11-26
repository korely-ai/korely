// What the card over the graph says of the selected node (NodeCard.tsx): run
// in node by scripts/check_the_behaviour.mjs (`npm test`), drawn with
// renderToString. Up to 1.0.39 its first word was the family of most of the
// node's facts, so a customer whose facts had relations of the family
// `other` read "OTHER", as if the person were classed as other.
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import type { Check } from "@/lib/behaviour";
import type { GNode } from "@/lib/graphTypes";
import { NodeCard } from "@/components/NodeCard";

const html = (el: ReactElement) => renderToString(el).replace(/<!-- -->/g, "");
/** The card's first word, where a reader looks for what the node is. */
const headline = (out: string) => /<span class="font-semibold uppercase tracking-wide"[^>]*>([^<]*)<\/span>/.exec(out)?.[1];
const node = (over: Partial<GNode>): GNode => ({ id: "Sofia", label: "Sofia", kind: "entity", degree: 3, ...over });
const card = (n: GNode, family = "other") =>
  html(<NodeCard node={n} family={family} familyColor="#94a3b8" links={2} neighbours={["Pro plan", "Rome"]} />);

export const CHECKS: Record<string, Check> = {
  "a person whose facts are of the family other is headed person, and other is said of the facts": (t) => {
    const out = card(node({ subjectType: "person" }));
    t.equal(headline(out), "person");
    t.ok(out.includes('Mostly <span class="text-foreground">other</span> facts'), out);
  },
  "a node of no known type is headed by its side of the facts, never by a family": (t) => {
    t.equal(headline(card(node({ subjectType: "unknown" }), "people")), "entity");
    t.equal(headline(card(node({ kind: "value", subjectType: undefined }), "work")), "value");
  },
  "the family of the facts is named once, beside its colour": (t) => {
    const out = card(node({ subjectType: "org" }), "work");
    t.equal(out.split(">work<").length - 1, 1, out);
    t.ok(out.includes("background:#94a3b8"), out);
  },
};
