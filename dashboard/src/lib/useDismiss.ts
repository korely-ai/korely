import { useEffect, useRef, type RefObject } from "react";

/**
 * A popover closes on a press outside it or on Escape, written once: the
 * project switcher and the requests' type filter carried two copies of the
 * same effect (audit of 17 Oct 2026), and a fix to one would have missed the
 * other, as it once did with the dialogs (components/Modal.tsx).
 *
 * `onDismiss` is read from a ref, so a caller may write it inline without the
 * listeners being taken down and put back at every render.
 */
export function useDismiss(
  ref: RefObject<HTMLElement>,
  open: boolean,
  onDismiss: () => void,
) {
  const dismiss = useRef(onDismiss);
  dismiss.current = onDismiss;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) dismiss.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss.current();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [ref, open]);
}
