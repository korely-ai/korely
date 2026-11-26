// Only the newest read may fill the screen.
//
// A page that reads again on every keystroke, filter or range button sends
// requests that can answer out of order: on a slow connection the answer to
// "a" may arrive after the one to "alice", and the list shown then belongs to
// a filter that is no longer on the screen (audit of 17 Oct 2026). Each read
// takes a ticket; an answer whose ticket is not the newest is dropped.
import { useCallback, useRef } from "react";

/** `const current = ticket()` before a read; `current()` is false once a
 *  newer read has started, and its answer must then not be shown. */
export function useLatest(): () => () => boolean {
  const seq = useRef(0);
  return useCallback(() => {
    const mine = ++seq.current;
    return () => mine === seq.current;
  }, []);
}
