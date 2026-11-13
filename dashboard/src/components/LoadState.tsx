import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import type { Load } from "@/lib/useLoad";

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

export function Loaded<T>({
  load,
  children,
  loadingText = "Loading…",
  className = "",
}: {
  load: Load<T>;
  children: (data: T) => ReactNode;
  loadingText?: string;
  className?: string;
}) {
  if (load.error) {
    return (
      <ErrorNote onRetry={load.reload} className={className}>
        {load.error}
      </ErrorNote>
    );
  }
  if (load.data === null) {
    return <p className={`text-sm text-muted-foreground ${className}`}>{loadingText}</p>;
  }
  return <>{children(load.data)}</>;
}
