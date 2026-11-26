import { useEffect, useState, type ReactNode } from "react";
import { AlertTriangle, Lock } from "lucide-react";
import type { Load } from "@/lib/useLoad";
import { BROWSER_CLOCK, asSentence, atMoment, type Clock, type Failure } from "@/lib/apiError";

/**
 * What a page shows for a read (lib/useLoad.ts) and for an action that did
 * not happen, written once (audit of 17 Oct 2026).
 *
 * `Loaded` draws its children only with data the server sent, so an empty
 * state inside them ("No endpoints yet") is an answer and never a failure in
 * disguise. A failed read says so, with the server's sentence when it gave
 * one, and offers to read again.
 */
export function ErrorNote({
  children,
  onRetry,
  className = "",
}: {
  children: ReactNode;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={`flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive ${className}`}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1">{children}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded px-2 py-0.5 text-xs font-medium underline-offset-2 hover:underline"
        >
          Try again
        </button>
      )}
    </div>
  );
}

/** The parts of a read a note about its failure needs. */
export type Failed = Pick<Load<unknown>, "error" | "reload" | "refused" | "waitUntil">;

/** Whether a failure may be offered again at `now` (milliseconds): never a
 *  403, and not before the end of a wait its answer named. */
export function offerAgain(again: Failure["again"], now: number): boolean {
  return again === "now" || (typeof again === "number" && now >= again);
}

/** Tells `show` whether the failure may be offered again now, and `true`
 *  when the wait it named is over, however long (lib/apiError.ts,
 *  `atMoment`). Returns a cancel. */
export function offerWhenDue(again: Failure["again"], show: (offer: boolean) => void, clock: Clock): () => void {
  show(offerAgain(again, clock.now()));
  if (typeof again !== "number" || offerAgain(again, clock.now())) return () => {};
  return atMoment(again, () => show(true), clock);
}

/** A failure, said: in red with "Try again" when trying again can help
 *  (once a wait it named is over: the note draws itself again then), and
 *  as what it is when it cannot, a refusal of who is asking (a 403), with
 *  no button that would fail forever (QA of 14 Nov 2026: Settings offered
 *  one to an operator who is not the install's administrator). `children`,
 *  when given, is said after it: what the page does without it. */
export function FailureNote({
  failure,
  onRetry,
  children,
  className = "",
}: {
  failure: Failure;
  onRetry?: () => void;
  children?: ReactNode;
  className?: string;
}) {
  const { again } = failure;
  const [offer, setOffer] = useState(() => offerAgain(again, Date.now()));
  useEffect(() => offerWhenDue(again, setOffer, BROWSER_CLOCK), [again]);
  const text = (
    <>
      {children ? asSentence(failure.text) : failure.text}
      {children ? <> {children}</> : null}
    </>
  );
  if (again === "never") {
    return (
      <div
        role="status"
        className={`flex items-start gap-2 rounded-md bg-secondary px-3 py-2 text-sm text-muted-foreground ${className}`}
      >
        <Lock className="mt-0.5 h-4 w-4 shrink-0" />
        <span className="min-w-0 flex-1">{text}</span>
      </div>
    );
  }
  return (
    <ErrorNote onRetry={onRetry && offer ? onRetry : undefined} className={className}>
      {text}
    </ErrorNote>
  );
}

/** A read's failure (lib/useLoad.ts) as a `FailureNote`, or nothing. */
export function LoadNote({
  load,
  children,
  className = "",
}: {
  load: Failed;
  children?: ReactNode;
  className?: string;
}) {
  if (!load.error) return null;
  const again: Failure["again"] = load.refused ? "never" : (load.waitUntil ?? "now");
  return (
    <FailureNote failure={{ text: load.error, again }} onRetry={load.reload} className={className}>
      {children}
    </FailureNote>
  );
}

/** The failures of the reads of one page, each cause said once: several
 *  reads refused together (a 429, the install stopped) drew a note each,
 *  three copies of one sentence on the Webhooks page (QA of 14 Nov 2026).
 *  Reads that failed alike share one note, whose "Try again" reads them
 *  all again; `more` is what the page does without one read, said in its
 *  note. */
export function LoadNotes({
  reads,
  className = "",
}: {
  reads: { load: Failed; more?: string }[];
  className?: string;
}) {
  const groups = new Map<string, { load: Failed; more?: string }[]>();
  for (const r of reads) {
    if (!r.load.error) continue;
    const key = `${r.load.error}|${r.load.refused}|${r.load.waitUntil}`;
    groups.set(key, [...(groups.get(key) ?? []), r]);
  }
  return (
    <>
      {[...groups.entries()].map(([key, group]) => {
        const first = group[0].load;
        const all: Failed = { ...first, reload: () => group.forEach((g) => g.load.reload()) };
        const more = group.map((g) => g.more).filter(Boolean).join(" ");
        return (
          <LoadNote key={key} load={all} className={className}>
            {more || undefined}
          </LoadNote>
        );
      })}
    </>
  );
}

export function Loaded<T>({
  load,
  children,
  loadingText = "Loading…",
  className = "",
  quiet = false,
}: {
  load: Load<T>;
  children: (data: T) => ReactNode;
  loadingText?: string;
  className?: string;
  quiet?: boolean;
}) {
  if (load.error) {
    // `quiet`: the page says this failure with its other reads (LoadNotes).
    return quiet ? null : <LoadNote load={load} className={className} />;
  }
  if (load.data === null) {
    return <p className={`text-sm text-muted-foreground ${className}`}>{loadingText}</p>;
  }
  return <>{children(load.data)}</>;
}
