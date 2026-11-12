import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Copy } from "lucide-react";

/**
 * Put text on the clipboard and say whether it got there.
 *
 * `navigator.clipboard` exists only in a secure context (HTTPS, or loopback).
 * An install opened over plain http on a LAN address has none, and the five
 * copy buttons this screen had before the audit of 17 Oct 2026 showed their
 * tick anyway (`navigator.clipboard?.writeText` does nothing and throws
 * nothing), or showed it when writeText refused, or threw and showed nothing.
 * One of them copies an API key shown once: a false "copied" there loses it.
 *
 * Off a secure context the browser's older copy command is tried, which
 * works there; when neither copies, the answer is false and the button says
 * to copy by hand.
 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Refused (no permission, the page not focused): try the other way.
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  try {
    area.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}

type Copied = "idle" | "copied" | "failed";

/** The copy button of every page: a tick only when the text was copied. */
export function CopyButton({
  text,
  label = "Copy",
  className = "",
  iconClassName = "h-3.5 w-3.5",
}: {
  text: string;
  label?: string;
  className?: string;
  iconClassName?: string;
}) {
  const [state, setState] = useState<Copied>("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    const ok = await copyText(text);
    setState(ok ? "copied" : "failed");
    window.clearTimeout(timer.current);
    // A failure stays until the next click: it asks for something to be done.
    if (ok) timer.current = window.setTimeout(() => setState("idle"), 1500);
  };

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={label}
      title={state === "failed" ? "Not copied: select the text and copy it by hand" : label}
      className={`inline-flex items-center gap-1 ${className}`}
    >
      {state === "copied" ? (
        <Check className={`${iconClassName} text-primary`} />
      ) : state === "failed" ? (
        <>
          <AlertTriangle className={`${iconClassName} text-destructive`} />
          <span className="text-[11px] text-destructive">Not copied: select it and copy</span>
        </>
      ) : (
        <Copy className={iconClassName} />
      )}
    </button>
  );
}
