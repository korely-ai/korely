import { useEffect, useId, useRef, useState, type MutableRefObject, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

/**
 * The one modal. There used to be five, each rolled by hand.
 *
 * That is the whole reason this file exists. A tester found that the page
 * behind a modal stayed operable through the overlay, and actually moved a live
 * API key by aiming at a menu inside the dialog and hitting one behind it. The
 * fix went onto the keys dialog. The projects dialog, the requests drawer, the
 * agents drawer and the memories panel each had their own copy of the same
 * markup and got nothing, so the same defect was still there four times over.
 *
 * Then the fix itself broke the dialog it was in, and that is worth writing
 * down because it is not obvious. `inert` went on `#root`, which is the right
 * subtree to freeze, except the dialog was rendered inside the React tree and
 * therefore inside `#root` as well. The attribute froze the dialog along with
 * the page: the form drew perfectly and nothing in it could be focused,
 * clicked or closed. Not even the X. The only way out was Escape, because that
 * listener is on the document, and nobody guesses Escape.
 *
 * `createPortal` into `document.body` is what makes the two separable: the
 * dialog leaves `#root`, so freezing `#root` freezes the page and not the
 * dialog. Everything else here is the announcement — a role, a name, the
 * initial focus, and Escape.
 *
 * The behaviour is written once, in `useOverlay`, and both `Modal` and
 * `ModalShell` call it: they used to carry two copies of the same effect,
 * the very pattern this file was made to end (audit of 17 Oct 2026).
 *
 * Verified in a real browser, which is the other half of the lesson: the
 * broken version compiled, type-checked, and shipped.
 */
// The overlays open now, the newest last. `#root` stays inert while any is
// open, and Escape closes only the newest: a dialog opened from a drawer used
// to take the drawer down with it, and closing it thawed the page under the
// drawer still open (audit of 17 Oct 2026).
const open: MutableRefObject<() => void>[] = [];
// Where the last overlay to close gave the focus back. A dialog that replaces
// another (the key form, then the key shown once) first renders while the
// focus is still in the one leaving, so what it saw as "before" is gone when
// it closes: it gives the focus where the first one gave it.
let lastGivenBack: HTMLElement | null = null;

/**
 * What every overlay does, in one place: freeze the page, close on Escape,
 * take the focus, and give it back on close.
 *
 * The effect runs once per overlay, not once per render. It used to depend on
 * `onClose`, which every caller writes inline, so each render of the page
 * behind it (a list refreshing, a key being copied) ran it again and threw
 * the focus back on the panel: a keyboard user tabbing to Copy in the
 * one-time key dialog was sent back to the top while the list reloaded. The
 * close is read from a ref instead, always the latest one.
 *
 * The panel takes the focus only when nothing inside it already has it, so
 * an input's `autoFocus` keeps it; and on close the focus goes back to what
 * had it before, the button that opened the overlay, instead of to the page's
 * top (audit of 17 Oct 2026).
 */
function useOverlay(panel: RefObject<HTMLDivElement>, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  // What had the focus, read at the first render: by the time an effect runs,
  // an `autoFocus` inside has already moved it into the dialog, and a focus
  // "given back" to that input, gone with the dialog, went nowhere (seen in
  // a real browser, 17 Oct 2026).
  const [before] = useState(() =>
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useEffect(() => {
    const root = document.getElementById("root");
    open.push(close);
    root?.setAttribute("inert", "");
    const onKey = (e: KeyboardEvent) => {
      const newest = open[open.length - 1] === close;
      if (!newest) return;
      if (e.key === "Escape") close.current();
      else if (e.key === "Tab") keepTheFocusIn(panel.current, e);
    };
    document.addEventListener("keydown", onKey);
    if (!panel.current?.contains(document.activeElement)) panel.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      open.splice(open.indexOf(close), 1);
      if (open.length === 0) root?.removeAttribute("inert");
      // After the page is thawed: an element under `inert` cannot take focus.
      const back = before?.isConnected ? before : lastGivenBack?.isConnected ? lastGivenBack : null;
      if (back) {
        back.focus();
        lastGivenBack = back;
      }
    };
  }, [panel, before]);
}

// What Tab can reach inside an overlay.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Tab and Shift+Tab go round inside the overlay. `#root` is inert, so the
 * page behind takes no focus, but past the overlay's last field the focus
 * left for the browser's own bar and the document, outside the dialog
 * (review of 28 Oct 2026, the menu's drawer).
 */
function keepTheFocusIn(panel: HTMLElement | null, e: KeyboardEvent) {
  if (!panel) return;
  const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (el) => el.getClientRects().length > 0,
  );
  const at = document.activeElement;
  if (items.length === 0) {
    e.preventDefault();
    panel.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const outside = !(at instanceof Node) || !panel.contains(at) || at === panel;
  if (e.shiftKey && (outside || at === first)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (outside || at === last)) {
    e.preventDefault();
    first.focus();
  }
}

export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useOverlay(panel, onClose);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onMouseDown={(e) => {
        // Only a click that starts AND ends on the backdrop closes. Starting
        // inside and releasing outside is a text selection that ran past the
        // edge, and treating that as "close" throws away what was typed.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`w-full ${wide ? "max-w-2xl" : "max-w-md"} rounded-lg border border-border bg-card p-5 shadow-xl outline-hidden`}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 id={titleId} className="text-base font-semibold">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}

/**
 * The same behaviour without the header, for overlays that have their own.
 *
 * The requests drawer, the agents drawer and the memories panel each lay out
 * their own top bar, so they cannot use `Modal` without being rewritten. They
 * can use this: the portal, the `inert`, the role, the name and the Escape,
 * around whatever markup they already had.
 *
 * `label` is what a screen reader announces, since there is no `<h2>` here to
 * point at.
 */
export function ModalShell({
  label,
  children,
  onClose,
  className = "fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4",
  id,
}: {
  label: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
  /** For the button that opens it (`aria-controls`). */
  id?: string;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useOverlay(panel, onClose);

  return createPortal(
    <div
      ref={panel}
      id={id}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      tabIndex={-1}
      className={`${className} outline-hidden`}
    >
      {children}
    </div>,
    document.body,
  );
}

/**
 * The foot of a dialog: what went wrong, a way back, and the action. Five
 * dialogs wrote these two buttons by hand, with the same classes in each
 * (audit of 17 Oct 2026); `danger` is the red one, for what cannot be undone.
 */
export function ModalActions({
  error,
  cancelLabel = "Cancel",
  onCancel,
  cancelDisabled = false,
  confirmLabel,
  onConfirm,
  confirmDisabled = false,
  danger = false,
}: {
  error?: string | null;
  cancelLabel?: string;
  onCancel: () => void;
  cancelDisabled?: boolean;
  confirmLabel: string;
  onConfirm: () => void;
  confirmDisabled?: boolean;
  danger?: boolean;
}) {
  return (
    <>
      {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={cancelDisabled}
          className="rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-secondary disabled:opacity-50"
        >
          {cancelLabel}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={confirmDisabled}
          className={
            danger
              ? "rounded-md bg-destructive px-3.5 py-2 text-sm font-medium text-destructive-foreground hover:opacity-90 disabled:opacity-50"
              : "rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          }
        >
          {confirmLabel}
        </button>
      </div>
    </>
  );
}

