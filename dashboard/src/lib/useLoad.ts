import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";
import { failure } from "@/lib/apiError";

/**
 * One read from the server, with the three states a screen must tell apart:
 * still reading, read, and could not read.
 *
 * Before the audit of 17 Oct 2026 every page wrote this by hand, and about a
 * dozen of them wrote the third state as `.catch(() => setX([]))` or
 * `.catch(() => setX(null))`: a failed read became an empty list ("No API
 * keys yet", "Get your first API key"), a zero ("0 Retrieval events") or a
 * "Loading…" that never ended. Here a failure is `error`, the server's own
 * sentence when it sent one (lib/apiError.ts) and `failed` otherwise, and
 * `data` stays null: nothing on screen can read it as an answer.
 *
 * A read started earlier that answers after a later one is dropped, so a
 * slow 30-day series cannot land on the 7-day chart picked since.
 *
 * `reload` reads again, keeping what is on screen until the answer comes.
 *
 * A failure is said the screen's one way (lib/apiError.ts, `failure`, QA of
 * 14 Nov 2026): a refusal that names a wait says until when, and is not
 * offered again before it (`waitUntil`; components/LoadState.tsx draws the
 * note again when it is over); a 403 is `refused`, and offered never.
 */
export interface Load<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  /** The failure was a 403: reading again with the same session cannot
   *  change it, so nothing offers to. */
  refused: boolean;
  /** Until when (milliseconds) the failure asked to wait; null when it
   *  named no wait. */
  waitUntil: number | null;
}

export function useLoad<T>(
  read: () => Promise<T>,
  deps: DependencyList,
  failed: string,
): Load<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refused, setRefused] = useState(false);
  const [waitUntil, setWaitUntil] = useState<number | null>(null);
  const latest = useRef(0);
  // The read of the latest render, so `reload` never calls a stale closure.
  const readRef = useRef(read);
  readRef.current = read;
  const failedRef = useRef(failed);
  failedRef.current = failed;

  const run = useCallback(() => {
    const mine = ++latest.current;
    setLoading(true);
    readRef
      .current()
      .then((value) => {
        if (mine !== latest.current) return;
        setData(value);
        setError(null);
        setRefused(false);
        setWaitUntil(null);
      })
      .catch((e) => {
        if (mine !== latest.current) return;
        const f = failure(e, failedRef.current, Date.now());
        setData(null);
        setError(f.text);
        setRefused(f.again === "never");
        setWaitUntil(typeof f.again === "number" ? f.again : null);
      })
      .finally(() => {
        if (mine === latest.current) setLoading(false);
      });
  }, []);

  // Read again whenever what the read depends on changes (the caller's deps).
  useEffect(run, deps);
  // A page left while its read is out does not take the answer.
  useEffect(() => () => void ++latest.current, []);
  return { data, error, loading, reload: run, refused, waitUntil };
}
