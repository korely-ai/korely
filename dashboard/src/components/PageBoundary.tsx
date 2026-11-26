import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

/**
 * A page that throws while it is drawn is said in its own place, and the
 * rest of the screen stays: the sidebar, the bar at the top, the other
 * pages. Without a boundary React unmounts the whole tree on any error in a
 * render or an effect, and the screen went white with nothing said (QA of
 * 14 Nov 2026: an answer that was not data on Memories, the graph in a
 * browser with no WebGL 2).
 *
 * Its parent gives it the page's address as its `key`: another address
 * mounts a new boundary, so leaving the page that broke is the way back
 * (React's error boundaries, getDerivedStateFromError, and the reset by key
 * the React documentation gives for them).
 */
interface Props {
  children: ReactNode;
}

interface State {
  /** What the page that broke said; null while it draws. */
  broken: string | null;
}

export class PageBoundary extends Component<Props, State> {
  state: State = { broken: null };

  static getDerivedStateFromError(error: unknown): State {
    return { broken: messageOf(error) };
  }

  componentDidCatch(_error: unknown, _info: ErrorInfo): void {
    // Nothing is sent anywhere: the screen has no error reporting, and what
    // a page held (memories, facts) must not leave it.
  }

  render() {
    if (this.state.broken !== null) return <PageBroke message={this.state.broken} />;
    return this.props.children;
  }
}

/** What an error said, for the person: its message, never its stack. */
export function messageOf(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return typeof error === "string" && error ? error : "unknown error";
}

/** The note a page that broke leaves in its place. */
export function PageBroke({ message }: { message: string }) {
  return (
    <div role="alert" className="mx-auto max-w-xl px-6 py-10">
      <div className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <div className="min-w-0 space-y-2">
          <p className="font-medium text-destructive">This page could not be drawn.</p>
          <p className="wrap-anywhere text-muted-foreground">
            It stopped on: {message}. Nothing was changed, and the other pages
            still work. Reload this one to try again.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-md border border-border bg-background px-3 py-1 text-xs font-medium hover:bg-secondary"
          >
            Reload
          </button>
        </div>
      </div>
    </div>
  );
}
