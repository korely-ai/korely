import { useEffect, useState } from "react";
import {
  FolderTree,
  Plus,
  Pencil,
  Archive,
  ArchiveRestore,
  Trash2,
  Check,
  X,
  KeyRound,
  Globe2,
  Hourglass,
} from "lucide-react";
import { PageHeader, EmptyState, HelpNote, fieldClass } from "@/components/PageShell";
import { CreateProjectDialog } from "@/components/CreateProjectDialog";
import { DeleteProjectDialog } from "@/components/DeleteProjectDialog";
import {
  RegionSelect,
  RegionHelp,
  REGION_LABEL,
  followsLabel,
  waitingReason,
} from "@/components/RegionSelect";
import { useMode } from "@/contexts/ModeContext";
import { useProjects } from "@/contexts/ProjectContext";
import { Loaded } from "@/components/LoadState";
import {
  projectsApi,
  errorCode,
  errorMessage,
  type AgentProject,
  type ProjectDeleteResult,
  type Region,
} from "@/lib/agentsApi";

// Map the backend's structured error codes onto copy a developer can act on;
// any other refusal in the server's words (a rename past the name's limit
// said "Something went wrong", QA of 14 Nov 2026).
function friendlyError(e: unknown): string {
  switch (errorCode(e)) {
    case "project_name_taken":
      return "A project with that name already exists. Pick another name.";
    case "cannot_archive_default":
      return "The Default project can't be archived: it's the fallback for keys.";
    case "project_has_live_keys":
      return "Move or revoke this project's live keys first, then archive it.";
    case "cannot_delete_default":
      return "The Default project can't be deleted: it's the fallback for keys.";
    default:
      return errorMessage(e) ?? "Nothing was changed. Try again.";
  }
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

// What a delete did, in the words of the road it took: moved to Default, or
// erased. Said out loud because it is the cheapest way to notice that a
// project somebody thought was empty was not.
function deletedSummary(name: string, r: ProjectDeleteResult): string {
  if (r.erasure === "permanent") {
    return (
      `Erased "${name}": ${plural(r.memories_deleted, "memory", "memories")}, ` +
      `${plural(r.facts_deleted, "fact", "facts")} and ` +
      `${plural(r.batches_deleted, "batch job", "batch jobs")} deleted for good. ` +
      `${plural(r.keys_moved, "key", "keys")} moved to Default.` +
      (r.keys_revoked > 0
        ? ` ${plural(r.keys_revoked, "key confined to its end users was", "keys confined to its end users were")} revoked.`
        : "") +
      (r.audit_id ? ` Audit record ${r.audit_id}.` : "")
    );
  }
  const notMoved =
    r.aliases_not_moved > 0
      ? ` ${plural(r.aliases_not_moved, "alias was", "aliases were")} not moved: ` +
        `Default had already decided ${r.aliases_not_moved === 1 ? "that name" : "those names"} differently.`
      : "";
  // Where what moved is written now: Default's region. The server refuses
  // a deletion that would move live keys or data into another region, but
  // a key that no longer works (expired, revoked) moves wherever Default
  // is, so this says Default's region, never "still".
  const region =
    r.region && r.keys_moved + r.memories_moved + r.facts_moved > 0
      ? ` Default writes in ${REGION_LABEL[r.region]}.`
      : "";
  return (
    `Deleted "${name}". Moved to Default: ${plural(r.keys_moved, "key", "keys")}, ` +
    `${plural(r.memories_moved, "memory", "memories")}, ` +
    `${plural(r.facts_moved, "fact", "facts")} and ` +
    `${plural(r.aliases_moved, "alias", "aliases")}.` +
    region +
    notMoved
  );
}

/** What Archive asks first: what it needs, and the way back. */
export function archiveQuestion(p: Pick<AgentProject, "name">): string {
  return `Archive "${p.name}"? Its keys must be moved or revoked first. Unarchive brings it back, here.`;
}

/** What the buttons of a row do, from what the page hands: Archive asks
 *  first and sends the archive; Unarchive asks nothing, since it takes
 *  nothing away, and sends the unarchive. */
export interface RowPage {
  edit: (p: AgentProject) => void;
  remove: (p: AgentProject) => void;
  ask: (question: string) => boolean;
  send: (p: AgentProject, call: (id: string) => Promise<unknown>) => void;
}

export function rowActions(page: RowPage): Record<"edit" | "archive" | "unarchive" | "remove", (p: AgentProject) => void> {
  return {
    edit: page.edit,
    remove: page.remove,
    archive: (p) => {
      if (page.ask(archiveQuestion(p))) page.send(p, projectsApi.archive);
    },
    unarchive: (p) => page.send(p, projectsApi.unarchive),
  };
}

/** What a row of the list offers. An archived project offers Unarchive where
 *  a live one offers Archive: the route was there (api/admin.py,
 *  unarchive_project) and the screen never called it, so a project archived
 *  here could only come back with an admin key (finding of 25 Nov 2026). */
export function ProjectActions({
  project: p,
  busy,
  page,
}: {
  project: AgentProject;
  busy: boolean;
  page: RowPage;
}) {
  // The page hands what it does (send, ask, edit, remove); which button sends
  // which route is said once, here, never at the page.
  const on = rowActions(page);
  return (
    <div className="hover-reveal flex shrink-0 items-center gap-1 transition-opacity">
      <button
        type="button"
        title="Edit"
        disabled={busy}
        onClick={() => on.edit(p)}
        className="rounded p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
      >
        <Pencil className="h-3.5 w-3.5" />
      </button>
      {!p.is_default && !p.archived && (
        <button
          type="button"
          title="Archive"
          disabled={busy}
          onClick={() => on.archive(p)}
          className="rounded p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
        >
          <Archive className="h-3.5 w-3.5" />
        </button>
      )}
      {!p.is_default && p.archived && (
        <button
          type="button"
          title="Unarchive"
          disabled={busy}
          onClick={() => on.unarchive(p)}
          className="rounded p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
        >
          <ArchiveRestore className="h-3.5 w-3.5" />
        </button>
      )}
      {p.is_default ? (
        <span
          title="The Default project is the fallback and can't be deleted."
          className="inline-flex cursor-not-allowed rounded p-1.5 text-muted-foreground/40"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </span>
      ) : (
        <button
          type="button"
          title="Delete"
          disabled={busy}
          onClick={() => on.remove(p)}
          className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}

export function ProjectsPage() {
  // The app's one list (contexts/ProjectContext.tsx), not a copy: what changes
  // here shows in the switcher at once (audit of 17 Oct 2026).
  // With the archived ones: they are here to be unarchived.
  const { withArchived: projects, projectsLoad, refresh } = useProjects();
  // The Cloud is an account on a service, not an install somebody owns.
  const { hosted } = useMode();
  // Read again on arrival: another tab, or a key moved, may have changed it.
  useEffect(() => {
    void refresh();
  }, [refresh]);
  // Action-level error, surfaced inline at the top (archive/delete codes).
  const [actionError, setActionError] = useState<string | null>(null);
  // What the last delete did, shown until the next action.
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<AgentProject | null>(null);

  // inline rename state
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ name: string; description: string; region: Region | null }>(
    { name: "", description: "", region: null },
  );
  const [busy, setBusy] = useState<string | null>(null);

  const startEdit = (p: AgentProject) => {
    setActionError(null);
    setEditing(p.id);
    setDraft({ name: p.name, description: p.description ?? "", region: p.region });
  };
  const cancelEdit = () => setEditing(null);

  const saveEdit = async (p: AgentProject) => {
    const patch: { name?: string; description?: string; region?: Region | null } = {};
    const name = draft.name.trim();
    const description = draft.description.trim();
    if (name && name !== p.name) patch.name = name;
    if (description !== (p.description ?? "")) patch.description = description;
    if (draft.region !== p.region) patch.region = draft.region;
    if (Object.keys(patch).length === 0) {
      setEditing(null);
      return;
    }
    setBusy(p.id);
    setActionError(null);
    try {
      await projectsApi.update(p.id, patch);
      setEditing(null);
      void refresh();
    } catch (e) {
      setActionError(friendlyError(e));
    } finally {
      setBusy(null);
    }
  };

  // Archive and Unarchive, one way: the row busy while it is sent, the list
  // read again after, a refusal said at the top.
  const send = async (p: AgentProject, call: (id: string) => Promise<unknown>) => {
    setBusy(p.id);
    setActionError(null);
    try {
      await call(p.id);
      void refresh();
    } catch (e) {
      setActionError(friendlyError(e));
    } finally {
      setBusy(null);
    }
  };

  // Deleting a project MOVES what it held to Default. Erasing is a separate
  // question, asked second, inside the same dialog and only from there: the
  // irreversible road must not be the one you take by accident, and one box
  // cannot mean both (components/DeleteProjectDialog.tsx). This comment said
  // so for a while before the page had any way to erase at all.
  const handleDelete = (p: AgentProject) => {
    setActionError(null);
    setNotice(null);
    setDeleting(p);
  };
  // What a row's buttons are handed (ProjectActions turns them into Archive
  // and Unarchive).
  const rowPage: RowPage = {
    edit: startEdit,
    remove: handleDelete,
    ask: (q) => confirm(q),
    send: (p, call) => void send(p, call),
  };

  // Default first, then alphabetical — the same ordering the keys page uses.
  const sorted = [...projects].sort((a, b) => {
    if (a.is_default !== b.is_default) return a.is_default ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  return (
    <div>
      <div className="mb-6 flex flex-col items-start gap-3 sm:flex-row sm:justify-between sm:gap-4">
        <PageHeader
          title="Projects"
          subtitle="A project scopes API keys and organizes your dashboard. Use one per product, or split staging from production."
        />
        <button
          type="button"
          onClick={() => {
            setActionError(null);
            setCreateOpen(true);
          }}
          className="mt-1 inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:bg-primary-hover"
        >
          <Plus className="h-4 w-4" />
          New project
        </button>
      </div>

      <HelpNote title="What a project isolates">
        <p className="mb-2">
          A project is a wall around data, not a label on it. A key belongs to
          one project, and with the memory scopes it reads, writes and deletes
          inside it only. Two projects can hold the same end user, the same
          agent name and the same fact, and neither will ever see the other's.
        </p>
        <p className="mb-2">
          This screen is the exception, and deliberately:{" "}
          {hosted
            ? "the account is yours, so the dashboard shows every project in it."
            : "you own the install, so the dashboard shows every project."}{" "}
          A key with the <span className="font-mono">admin</span> scope sees
          them all too, whatever its project: through the administration API
          it reads and deletes the memories of every project of the account.
          Keep admin keys for your own scripts, never in the tools of one
          customer or one project.
        </p>
        <p>
          The <span className="text-foreground">Default</span> project is where
          every key lands unless you pick another, and where data written before
          projects existed now lives. Deleting a project does not
          delete its keys: they fall back to Default, so a running agent never
          stops because somebody tidied up. Its memories, facts and aliases
          move there too, unless you choose to erase them, which the delete
          dialog asks as a second, separate step.
        </p>
      </HelpNote>

      {actionError && (
        <p className="mb-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {actionError}
        </p>
      )}
      {notice && (
        <p className="mb-4 rounded-md bg-secondary px-3 py-2 text-sm" role="status">
          {notice}
        </p>
      )}

      {/* The list is drawn only from an answer: a failed read says so, with
          "Try again", and is never "No projects yet". */}
      <Loaded load={projectsLoad}>
        {() => sorted.length === 0 ? (
        <EmptyState icon={<FolderTree className="h-6 w-6" />} title="No projects yet">
          Create a project to group your keys, for example one for staging and
          one for production.
        </EmptyState>
      ) : (
        <ul className="space-y-2">
          {sorted.map((p) => {
            const isEditing = editing === p.id;
            const isBusy = busy === p.id;
            return (
              <li
                key={p.id}
                className={`group rounded-lg border border-border bg-card px-4 py-3.5 ${p.archived ? "opacity-60" : ""}`}
              >
                {isEditing ? (
                  <div className="space-y-2">
                    <input
                      autoFocus
                      value={draft.name}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d, name: e.target.value }))
                      }
                      onKeyDown={(e) => e.key === "Enter" && saveEdit(p)}
                      placeholder="Project name"
                      className={`w-full ${fieldClass}`}
                    />
                    <input
                      value={draft.description}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d, description: e.target.value }))
                      }
                      onKeyDown={(e) => e.key === "Enter" && saveEdit(p)}
                      placeholder="Description (optional)"
                      className={`w-full ${fieldClass}`}
                    />
                    <div>
                      <RegionSelect
                        noneStored={p.region === null}
                        value={draft.region}
                        onChange={(region) => setDraft((d) => ({ ...d, region }))}
                      />
                      <RegionHelp className="mt-1 text-xs text-muted-foreground" />
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        title="Save"
                        disabled={isBusy}
                        onClick={() => saveEdit(p)}
                        className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
                      >
                        <Check className="h-3.5 w-3.5" />
                        Save
                      </button>
                      <button
                        type="button"
                        title="Cancel"
                        onClick={cancelEdit}
                        className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-secondary"
                      >
                        <X className="h-3.5 w-3.5" />
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{p.name}</span>
                        <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                          {p.slug}
                        </span>
                        {p.is_default && (
                          <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-foreground">
                            Default
                          </span>
                        )}
                        {p.archived && (
                          <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                            Archived
                          </span>
                        )}
                        {p.processing_region ? (
                          <span
                            title={p.region ? "Chosen for this project" : followsLabel(hosted)}
                            className="inline-flex items-center gap-1 text-xs text-muted-foreground"
                          >
                            <Globe2 className="h-3 w-3" />
                            {REGION_LABEL[p.processing_region]}
                          </span>
                        ) : (
                          <span
                            title={waitingReason(hosted)}
                            className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 text-xs text-amber-700 dark:text-amber-400"
                          >
                            <Hourglass className="h-3 w-3" />
                            {p.region
                              ? `No provider for ${REGION_LABEL[p.region]} yet`
                              : "No provider yet"}
                          </span>
                        )}
                        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                          <KeyRound className="h-3 w-3" />
                          {p.key_count} {p.key_count === 1 ? "key" : "keys"}
                        </span>
                      </div>
                      {p.description && (
                        <p className="mt-1 text-sm text-muted-foreground">
                          {p.description}
                        </p>
                      )}
                    </div>

                    <ProjectActions project={p} busy={isBusy} page={rowPage} />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      </Loaded>

      {deleting && (
        <DeleteProjectDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={(result) => {
            setNotice(deletedSummary(deleting.name, result));
            setDeleting(null);
            void refresh();
          }}
        />
      )}

      {createOpen && (
        <CreateProjectDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false);
            void refresh();
          }}
        />
      )}
    </div>
  );
}
