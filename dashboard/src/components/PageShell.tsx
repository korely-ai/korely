import { useState, type ReactNode } from "react";
import { BookOpen, ChevronDown, ChevronRight, Info } from "lucide-react";
import { useMode } from "@/contexts/ModeContext";
import { useProjectScope } from "@/contexts/ProjectContext";
import type { DocsTarget } from "@/lib/docsLinks";

/** The text field and the select of every form and filter bar, one class
 *  (audit of 17 Oct 2026: it was written out eighteen times, so a change to
 *  the focus ring meant eighteen edits). Width and margin are the caller's. */
export const fieldClass =
  "rounded-md border border-input bg-background px-3 py-2 text-sm outline-hidden focus:ring-2 focus:ring-ring";

/** Page title + subtitle, with an optional "Docs" link, shared by every page. */
export function PageHeader({
  title,
  subtitle,
  docs,
}: {
  title: string;
  subtitle?: string;
  docs?: DocsTarget;
}) {
  return (
    // Stacked below `sm`, so the subtitle has the width of a phone and the
    // docs link goes under it (functional test of 28 Oct 2026, 390 px).
    <div className="mb-6 flex flex-col items-start gap-3 sm:flex-row sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && (
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{subtitle}</p>
        )}
      </div>
      {docs && (
        <a
          href={docs.href}
          target="_blank"
          rel="noreferrer"
          className="mt-1 inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <BookOpen className="h-3.5 w-3.5" />
          {docs.label}
        </a>
      )}
    </div>
  );
}

/**
 * Collapsible "how this works" explainer. Use where an inline teaching block
 * helps but would clutter if always open (graph legend, webhooks intro).
 */
export function HelpNote({
  title,
  children,
  defaultOpen = false,
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mb-5 rounded-lg border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm font-medium"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted-foreground" />
        )}
        {title}
      </button>
      {open && (
        <div className="border-t border-border px-4 py-3 text-sm leading-relaxed text-muted-foreground">
          {children}
        </div>
      )}
    </div>
  );
}

/**
 * Which project a data page shows: the one chosen at the top, or every project
 * when "All projects" is chosen (contexts/ProjectContext.tsx `scope`). Every
 * page under InProjectScope draws it under its header.
 *
 * Until 2026-09-12 this banner said that projects organized keys and that
 * memories and facts were account-wide; then a key was confined to its project
 * and the banner said this screen was not, "every project", on every page that
 * drew it, whatever the switcher said, and the pages that did not draw it read
 * every project too (6 Nov 2026: the Memory Graph drew the account's 2,808
 * facts under a project of 9). The screen reads what it names now. The whole
 * account stays one choice away, said as such, because it belongs to the
 * person who owns it; on the Cloud that is an account, not an install (audit
 * of 17 Oct 2026). `oneProjectNote`: what the page shows of the account
 * anyway, said only while one project is shown.
 */
export function ProjectScopeBanner({ oneProjectNote }: { oneProjectNote?: ReactNode } = {}) {
  const { hosted } = useMode();
  const scope = useProjectScope();
  return (
    <div className="mb-5 flex items-start gap-2.5 rounded-lg border border-border bg-secondary/40 px-4 py-3 text-sm text-muted-foreground">
      <Info className="mt-0.5 h-4 w-4 shrink-0" />
      {scope.kind === "project" ? (
        <p>
          This screen shows the project{" "}
          <span className="font-medium text-foreground">{scope.name}</span>, as its API keys read it.
          Choose another project, or All projects, at the top.
          {oneProjectNote && <> {oneProjectNote}</>}
        </p>
      ) : (
        <p>
          This screen shows{" "}
          <span className="font-medium text-foreground">every project</span>{" "}
          {hosted ? "in your account" : "on this install"}. An API key does not: it is confined to the project it
          belongs to, and cannot read, write or delete outside it.
        </p>
      )}
    </div>
  );
}

/**
 * Honest empty state. The shell, routing, and auth are real; data arrives per
 * slice.
 */
export function EmptyState({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-card/40 px-6 py-16 text-center">
      <div className="mb-3 text-muted-foreground">{icon}</div>
      <p className="text-sm font-medium">{title}</p>
      {children && (
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">{children}</p>
      )}
    </div>
  );
}
