import { Link } from "react-router";
import { Clock } from "lucide-react";
import type { AgentKey } from "@/lib/agentsApi";
import { overlapInForce, type EndNear, type OverlapChoice } from "@/lib/keyLife";
import { fieldClass } from "@/components/PageShell";

// What the API Keys page and Home draw of a key's end (lib/keyLife.ts
// decides it): the badge beside a key's date, the old secret's line under a
// rotated key's name, Home's line when a key ends soon, and Rotate's
// question of when the old secret stops.

const AMBER = "bg-amber-500/10 text-amber-700 dark:text-amber-400";
const RED = "bg-destructive/10 text-destructive";

/** "Ends in 3 days" beside the date, in red on its last day; nothing when
 *  the end is not near. */
export function EndBadge({ near }: { near: EndNear | null }) {
  if (!near) return null;
  return (
    <span
      title={`It stops working on ${new Date(near.at).toLocaleString()}. Rotate it to give it a new secret.`}
      className={`ml-2 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${near.lastDay ? RED : AMBER}`}
    >
      Ends {near.words}
    </span>
  );
}

/** Under a rotated key's name, while the secret it replaced still works. */
export function OldSecretLine({ until }: { until: number | null }) {
  if (until === null) return null;
  return (
    <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
      Old secret works until {new Date(until).toLocaleString()}
    </span>
  );
}

const SHOWN = 3;

/** Home's line when keys end soon: which, how soon, and where to rotate
 *  them. Nothing when none does. */
export function KeysEndingNote({
  ending,
  className = "",
}: {
  ending: { key: AgentKey; near: EndNear }[];
  className?: string;
}) {
  if (ending.length === 0) return null;
  const urgent = ending.some((e) => e.near.lastDay);
  const named = ending
    .slice(0, SHOWN)
    .map((e) => `${e.key.name || e.key.masked} ${e.near.words}`)
    .join(", ");
  const more = ending.length > SHOWN ? ` and ${ending.length - SHOWN} more` : "";
  return (
    <div
      role="status"
      className={`flex items-start gap-2 rounded-md px-3 py-2 text-sm ${urgent ? RED : AMBER} ${className}`}
    >
      <Clock className="mt-0.5 h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1">
        {ending.length === 1 ? "An API key stops working soon" : `${ending.length} API keys stop working soon`}
        : {named}
        {more}.{" "}
        <Link to="/keys" className="font-medium underline underline-offset-2">
          Rotate {ending.length === 1 ? "it" : "them"} on API Keys
        </Link>
      </span>
    </div>
  );
}

/** Rotate's question, when the old secret stops: the server's choices
 *  (`offer`, lib/keyLife.ts overlapChoices), the menu on the choice in
 *  force (`overlapInForce`), which is the one the dialog sends, and what it
 *  means under it. */
export function OldSecretChoice({
  offer,
  picked,
  onPick,
}: {
  offer: { choices: OverlapChoice[]; start: number | undefined };
  picked: number | undefined | null;
  onPick: (hours: number | undefined) => void;
}) {
  const hours = overlapInForce(offer, picked);
  const valueOf = (h: number | undefined) => (h === undefined ? "default" : String(h));
  return (
    <>
      <label htmlFor="old-secret" className="mb-1.5 block text-sm font-medium">
        The old secret stops
      </label>
      <select
        id="old-secret"
        value={valueOf(hours)}
        onChange={(e) => onPick(offer.choices.find((c) => valueOf(c.hours) === e.target.value)?.hours)}
        className={`mb-1 w-full ${fieldClass}`}
      >
        {offer.choices.map((c) => (
          <option key={valueOf(c.hours)} value={valueOf(c.hours)}>
            {c.hours !== 0 && c.hours !== undefined ? `In ${c.label}` : c.label}
          </option>
        ))}
      </select>
      <p className="mb-4 text-xs text-muted-foreground">
        {hours === 0
          ? "At once: the agents that hold it stop until they get the new one. Choose this for a key that leaked."
          : "Until then both secrets work, so your agents can switch. For a key that leaked, choose Now."}
      </p>
    </>
  );
}
