import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";
import { errorMessage } from "@/lib/agentsApi";

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
 */
export interface Load<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

export function useLoad<T>(
  read: () => Promise<T>,
  deps: DependencyList,
  failed: string,
): Load<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
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
      })
      .catch((e) => {
        if (mine !== latest.current) return;
        setData(null);
        setError(errorMessage(e) ?? failedRef.current);
      })
      .finally(() => {
        if (mine === latest.current) setLoading(false);
      });
  }, []);

  // Read again whenever what the read depends on changes (the caller's deps).
  useEffect(run, deps);
  // A page left while its read is out does not take the answer.
  useEffect(() => () => void ++latest.current, []);

  return { data, error, loading, reload: run };
}
