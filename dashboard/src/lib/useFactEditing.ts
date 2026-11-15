import { useState } from "react";
import { factsApi, type Fact } from "@/lib/agentsApi";

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

  const forget = async (f: Fact) => {
    setBusy(f.id);
    setErr(null);
    try {
      await factsApi.forget(f.id);
      reload();
    } catch {
      setErr("Could not forget the fact.");
    } finally {
      setBusy(null);
    }
  };

  return { editing, draft, setDraft, busy, err, startEdit, cancel, save, forget };
}
