import { useRef, useState } from "react";
import { Check, ChevronsUpDown, FolderTree, Plus, Search } from "lucide-react";
import { cn } from "@/lib/cn";
import { useProjects } from "@/contexts/ProjectContext";
import { CreateProjectDialog } from "@/components/CreateProjectDialog";
import { ErrorNote } from "@/components/LoadState";
import { useDismiss } from "@/lib/useDismiss";

/**
 * Top-left project switcher — the Mem0 / Vercel / Stripe pattern. Picks the
 * active project, creates a new one inline, and shows the account plan as a
 * pill. Keys land in the active project by default; the create flow is the
 * same modal the Projects page uses.
 */
export function ProjectSwitcher() {
  const { projects, projectsLoad, activeProject, setActiveProjectId, refresh } = useProjects();
  const [open, setOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  // There was an account plan badge here. A private install has no plan.

  useDismiss(ref, open, () => setOpen(false));

  const live = projects.filter((p) => !p.archived);
  const visible = live
    .filter((p) => p.name.toLowerCase().includes(query.trim().toLowerCase()))
    .sort((a, b) => {
      if (a.is_default !== b.is_default) return a.is_default ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  const showSearch = live.length > 6;

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm transition-colors hover:bg-secondary"
      >
        <FolderTree className="h-4 w-4 text-muted-foreground" />
        <span className="max-w-48 truncate font-medium">
          {activeProject?.name ?? "Select project"}
        </span>
        <ChevronsUpDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1.5 w-72 overflow-hidden rounded-lg border border-border bg-card shadow-lg">
          {showSearch && (
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
              <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search projects"
                className="w-full bg-transparent text-sm outline-hidden placeholder:text-muted-foreground"
              />
            </div>
          )}

          {/* A failed read is not "No projects found." (audit of 17 Oct 2026). */}
          {projectsLoad.error && (
            <ErrorNote onRetry={projectsLoad.reload} className="m-2">
              {projectsLoad.error}
            </ErrorNote>
          )}
          {/* A list of buttons, not a listbox: role="listbox" promised the
              arrow keys and the options of a select, and around buttons it
              announced options that Tab skipped (audit of 17 Oct 2026). */}
          <ul className="max-h-72 overflow-y-auto py-1">
            {projectsLoad.data === null ? (
              projectsLoad.error ? null : (
                <li className="px-3 py-2 text-sm text-muted-foreground">Loading…</li>
              )
            ) : visible.length === 0 ? (
              <li className="px-3 py-2 text-sm text-muted-foreground">
                No projects found.
              </li>
            ) : (
              visible.map((p) => {
                const active = p.id === activeProject?.id;
                return (
                  <li key={p.id}>
                    <button
                      type="button"
                      aria-current={active}
                      onClick={() => {
                        setActiveProjectId(p.id);
                        setOpen(false);
                        setQuery("");
                      }}
                      className={cn(
                        "flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-secondary",
                        active && "bg-secondary/60",
                      )}
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <FolderTree className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                        <span className="truncate">{p.name}</span>
                        {p.is_default && (
                          <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-primary">
                            Default
                          </span>
                        )}
                      </span>
                      {active && <Check className="h-4 w-4 shrink-0 text-primary" />}
                    </button>
                  </li>
                );
              })
            )}
          </ul>

          <button
            type="button"
            onClick={() => {
              setOpen(false);
              setCreateOpen(true);
            }}
            className="flex w-full items-center gap-2 border-t border-border px-3 py-2.5 text-sm font-medium transition-colors hover:bg-secondary"
          >
            <Plus className="h-4 w-4" />
            New project
          </button>
        </div>
      )}

      {createOpen && (
        <CreateProjectDialog
          onClose={() => setCreateOpen(false)}
          onCreated={(project) => {
            setCreateOpen(false);
            // Make the new project active and read the list again: the
            // selection is kept once the list that holds it arrives.
            setActiveProjectId(project.id);
            refresh();
          }}
        />
      )}
    </div>
  );
}
