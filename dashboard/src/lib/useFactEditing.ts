import { useState } from "react";
import { factsApi, type Fact } from "@/lib/agentsApi";

/** What a person is asked before a fact is forgotten: which fact, and what
 *  forgetting does to it. */
export function forgetQuestion(f: Pick<Fact, "subject" | "predicate" | "object">): string {
  return (
    `Forget "${f.subject} ${f.predicate} ${f.object}"? It stops being current and stays in the ` +
    "history. The dashboard can't make it current again."
  );
}

/** What forgetting a fact does to the screen and the server, in order. */
export interface ForgetSteps {
  /** Asks the person; true is a yes. */
  ask: (question: string) => boolean;
  /** Which fact's buttons are disabled while it is sent; null, none. */
  busy: (id: string | null) => void;
  err: (text: string | null) => void;
  send: (id: string) => Promise<unknown>;
  reload: () => void;
}

/**
 * Forgetting changes the data and the dashboard cannot make the fact current
 * again, so it is asked first, as deleting a memory, revoking a key and
 * archiving a project are: one tap on a phone, on buttons only a mouse could
 * reveal, forgot a fact with no question (pre-tester check of 15 Nov 2026).
 * Until a yes nothing changes, on the screen either: a fact marked busy
 * before the question kept its buttons disabled after a no.
 */
export async function forgetAsking(f: Fact, s: ForgetSteps): Promise<void> {
  if (!s.ask(forgetQuestion(f))) return;
  s.busy(f.id);
  s.err(null);
  try {
    await s.send(f.id);
    s.reload();
  } catch {
    s.err("Could not forget the fact.");
  } finally {
    s.busy(null);
  }
}

/**
 * Shared forget/correct logic for any view that lists facts (Facts page,
 * Graph side panel). Keeps the two surfaces consistent: a correction is a
 * supersede (new fact + old invalidated), a forget is an invalidate — both
 * preserve the bi-temporal record. `reload` is called after a mutation lands.
 */
export function useFactEditing(reload: () => void) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState({ subject: "", predicate: "", object: "" });
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const startEdit = (f: Fact) => {
    setEditing(f.id);
    setDraft({ subject: f.subject, predicate: f.predicate, object: f.object });
  };
  const cancel = () => setEditing(null);

  const save = async (f: Fact) => {
    const patch: { subject?: string; predicate?: string; object?: string } = {};
    if (draft.subject.trim() && draft.subject.trim() !== f.subject) patch.subject = draft.subject.trim();
    if (draft.predicate.trim() && draft.predicate.trim() !== f.predicate) patch.predicate = draft.predicate.trim();
    if (draft.object.trim() && draft.object.trim() !== f.object) patch.object = draft.object.trim();
    if (Object.keys(patch).length === 0) {
      setEditing(null);
      return;
    }
    setBusy(f.id);
    setErr(null);
    try {
      await factsApi.correct(f.id, patch);
      setEditing(null);
      reload();
    } catch {
      setErr("Could not correct the fact.");
    } finally {
      setBusy(null);
    }
  };

  const forget = (f: Fact) =>
    forgetAsking(f, {
      ask: (q) => confirm(q),
      busy: setBusy,
      err: setErr,
      send: factsApi.forget,
      reload,
    });

  return { editing, draft, setDraft, busy, err, startEdit, cancel, save, forget };
}
