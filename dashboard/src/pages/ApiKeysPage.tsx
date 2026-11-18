import { useEffect, useRef, useState } from "react";
import { KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { PageHeader, EmptyState, fieldClass } from "@/components/PageShell";
import { Modal, ModalActions } from "@/components/Modal";
import { cn } from "@/lib/cn";
import {
  endUsersFrom,
  errorMessage,
  keysApi,
  type AgentKey,
  type AgentKeyCreated,
  type AgentProject,
  type KeyLifetimes,
} from "@/lib/agentsApi";
import { useLoad, type Load } from "@/lib/useLoad";
import {
  afterRotation,
  endNear,
  oldSecretUntil,
  overlapChoices,
  overlapInForce,
  rotateFromTheScreen,
} from "@/lib/keyLife";
import { EndBadge, OldSecretChoice, OldSecretLine } from "@/components/KeyEnds";
import { ErrorNote, LoadNote, Loaded } from "@/components/LoadState";
import { CopyButton } from "@/components/CopyButton";
import { useProjects } from "@/contexts/ProjectContext";
import { DOCS } from "@/lib/docsLinks";

export function ApiKeysPage() {
  // New keys default to the project picked in the top-left switcher. The
  // projects are the app's one list (contexts/ProjectContext.tsx): this page
  // read a copy of its own, which a rename or a delete on the Projects page
  // left behind (audit of 17 Oct 2026, on the Projects page).
  const { activeProjectId, projects, projectsLoad, refresh: refreshProjects } = useProjects();
  // Read again on arrival, as the copy was: another tab may have changed it.
  useEffect(() => {
    refreshProjects();
  }, [refreshProjects]);
  const [createOpen, setCreateOpen] = useState(false);
  // The full key, shown once: a new key, or a rotated key's new secret.
  const [revealed, setRevealed] = useState<{ key: AgentKeyCreated; rotated: boolean } | null>(null);
  const [rotating, setRotating] = useState<AgentKey | null>(null);
  // What a new key may last, the notice before a key ends and what Rotate
  // offers for the old secret: read once for the page, from the server.
  const lifetimes = useLoad(keysApi.lifetimes, [], "The key lifetimes could not be read.");

  // A failed read is said as one (lib/useLoad.ts). It used to set an error
  // above an empty list, so the page read "No API keys yet" under it, and
  // the error stayed after a later read had worked (audit of 17 Oct 2026).
  const listing = useLoad(keysApi.list, [], "Could not load your API keys.");
  const refresh = listing.reload;
  // What a revoke or a move answered when it did not happen. Both were
  // awaited with nothing around them, so a refusal or a dropped connection
  // left the row as it was and the reason in the console only: whoever
  // revoked a leaked key was not told it still works (audit of 17 Oct 2026).
  const [actionError, setActionError] = useState<string | null>(null);

  const handleCreated = (k: AgentKeyCreated) => {
    setCreateOpen(false);
    setRevealed({ key: k, rotated: false }); // show the full key once
    refresh();
  };

  const handleRotated = (k: AgentKeyCreated) => {
    setRotating(null);
    setRevealed({ key: k, rotated: true }); // the new secret, once
    refresh();
  };

  const handleRevoke = async (id: string) => {
    if (!confirm("Revoke this key? Agents using it will stop working immediately.")) {
      return;
    }
    setActionError(null);
    try {
      await keysApi.revoke(id);
    } catch (e) {
      setActionError(
        errorMessage(e) ?? "The key was not revoked: it still works. Try again.",
      );
      return;
    }
    refresh();
  };

  const handleMove = async (id: string, projectId: string, name: string) => {
    // Ask first. A project is an isolation wall, so this moves a live
    // credential from one side of it to the other: the agents running on that
    // key stop seeing what they were reading and start seeing something else.
    //
    // It used to happen on one click with no confirmation and no feedback. A
    // tester triggered it by accident, through an overlay that should not have
    // let the control be reachable at all, and only found out by reading the
    // database afterwards.
    const to = projects.find((p) => p.id === projectId);
    if (!confirm(
      `Move the key "${name}" to "${to?.name ?? "that project"}"? ` +
        `It stops seeing its current project's memories and facts, and starts ` +
        `seeing that one's.`,
    )) {
      return;
    }
    setActionError(null);
    try {
      await keysApi.move(id, projectId);
    } catch (e) {
      setActionError(
        errorMessage(e) ?? "The key was not moved: it still reads its current project. Try again.",
      );
      return;
    }
    refresh();
  };

  return (
    <div>
      <div className="mb-8 flex flex-col items-start gap-3 sm:flex-row sm:justify-between">
        <PageHeader
          title="API Keys"
          subtitle="Create and manage the keys your agents use to call Korely."
          docs={DOCS.keys}
        />
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
        >
          <Plus className="h-4 w-4" />
          Create key
        </button>
      </div>

      {actionError && <ErrorNote className="mb-4">{actionError}</ErrorNote>}
      {/* Without the projects a key can be neither moved nor created in one. */}
      <LoadNote load={projectsLoad} className="mb-4" />
      <LoadNote load={lifetimes} className="mb-4">
        No key is marked as ending soon, and Rotate offers only Now and this install's default.
      </LoadNote>

      <Loaded load={listing}>
        {(keys) =>
          keys.length === 0 ? (
            <EmptyState icon={<KeyRound className="h-6 w-6" />} title="No API keys yet">
              Generate a key to authenticate your agent's requests to the Korely
              memory API.
            </EmptyState>
          ) : (
            <GroupedKeys
              keys={keys}
              projects={projects}
              onRevoke={handleRevoke}
              onMove={handleMove}
              onRotate={setRotating}
              warningDays={lifetimes.data?.warning_days ?? null}
            />
          )
        }
      </Loaded>

      {createOpen && (
        <CreateKeyDialog
          projects={projects}
          defaultProjectId={activeProjectId}
          lifetimes={lifetimes}
          onClose={() => setCreateOpen(false)}
          onCreated={handleCreated}
        />
      )}
      {rotating && (
        <RotateKeyDialog
          target={rotating}
          lifetimes={lifetimes.data}
          onClose={() => setRotating(null)}
          onRotated={handleRotated}
          onFailed={refresh}
        />
      )}
      {revealed && (
        <RevealKeyDialog
          keyValue={revealed.key}
          rotated={revealed.rotated}
          onClose={() => setRevealed(null)}
        />
      )}
    </div>
  );
}

const DEFAULT_GROUP = "Default";

/** Group key rows under per-project section headers, Default first. */
function GroupedKeys({
  keys,
  projects,
  onRevoke,
  onMove,
  onRotate,
  warningDays,
}: {
  keys: AgentKey[];
  projects: AgentProject[];
  onRevoke: (id: string) => void;
  onMove: (id: string, projectId: string, name: string) => void;
  onRotate: (k: AgentKey) => void;
  warningDays: number | null;
}) {
  // Bucket by project name (falling back to "Default" for null project).
  const buckets = new Map<string, AgentKey[]>();
  for (const k of keys) {
    const name = k.project_name ?? DEFAULT_GROUP;
    const list = buckets.get(name);
    if (list) list.push(k);
    else buckets.set(name, [k]);
  }

  const names = [...buckets.keys()].sort((a, b) => {
    if (a === DEFAULT_GROUP) return -1;
    if (b === DEFAULT_GROUP) return 1;
    return a.localeCompare(b);
  });

  return (
    <div className="space-y-6">
      {names.map((name) => (
        <section key={name}>
          <h2 className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            {name}
            {name === DEFAULT_GROUP && (
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium tracking-wider text-primary">
                Default
              </span>
            )}
          </h2>
          <KeyTable
            keys={buckets.get(name)!}
            projects={projects}
            onRevoke={onRevoke}
            onMove={onMove}
            onRotate={onRotate}
            warningDays={warningDays}
          />
        </section>
      ))}
    </div>
  );
}

function KeyTable({
  keys,
  projects,
  onRevoke,
  onMove,
  onRotate,
  warningDays,
}: {
  keys: AgentKey[];
  projects: AgentProject[];
  onRevoke: (id: string) => void;
  onMove: (id: string, projectId: string, name: string) => void;
  onRotate: (k: AgentKey) => void;
  warningDays: number | null;
}) {
  // Only non-archived projects are valid move targets.
  const moveTargets = projects.filter((p) => !p.archived);
  const now = Date.now();
  // Below `md` each key is a card, its cells one under the other with their
  // names: as a table of five columns a phone showed the name and the key,
  // and the expiry, the project and Revoke only to who dragged the table
  // sideways (at 390 px). One markup, so the
  // two layouts cannot say different things.
  const label = "mr-1.5 text-xs uppercase tracking-wider text-muted-foreground md:hidden";
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="block w-full text-sm md:table">
        <thead className="hidden bg-secondary/50 text-left text-xs uppercase tracking-wider text-muted-foreground md:table-header-group">
          <tr>
            <th className="px-4 py-2.5 font-medium">Name</th>
            <th className="px-4 py-2.5 font-medium">Key</th>
            <th className="px-4 py-2.5 font-medium">Last used</th>
            <th className="px-4 py-2.5 font-medium">Expires</th>
            <th className="px-4 py-2.5 font-medium" />
          </tr>
        </thead>
        <tbody className="block divide-y divide-border md:table-row-group">
          {keys.map((k) => (
            <tr key={k.id} className={cn("block py-2 md:table-row md:py-0", k.revoked && "opacity-50")}>
              <td className="block px-4 py-1.5 font-medium md:table-cell md:py-3">
                {k.name || <span className="text-muted-foreground">Untitled</span>}
                {k.revoked && (
                  <span className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                    Revoked
                  </span>
                )}
                {/* The server's state (keys.state_of): a key every door refuses
                    or one past its date is not shown as working. */}
                {!k.revoked && (k.state === "refused" || k.state === "expired") && (
                  <span
                    className="ml-2 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-amber-700 dark:text-amber-400"
                    title={k.state === "refused"
                      ? "Its prefix is the Cloud's: this install refuses it. Create a new key and revoke this one."
                      : "Past its expiry date: it no longer works."}
                  >
                    {k.state === "refused" ? "Refused here" : "Expired"}
                  </span>
                )}
                {/* What it may do, when it is not the usual read and write. */}
                {k.scopes.includes("admin") ? (
                  <span className="ml-2 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-amber-700 dark:text-amber-400">
                    Admin
                  </span>
                ) : !k.scopes.includes("memories:write") ? (
                  <span className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                    Read only
                  </span>
                ) : !k.scopes.includes("memories:read") ? (
                  <span className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                    Write only
                  </span>
                ) : null}
                {/* Confined to some end users of its project. */}
                {k.users && k.users.length > 0 && (
                  <span
                    title={k.users.join(", ")}
                    className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-[10px] tracking-wider text-muted-foreground"
                  >
                    Only {k.users.join(", ")}
                  </span>
                )}
                {/* A rotation's overlap, while the old secret still works. */}
                <OldSecretLine until={oldSecretUntil(k, now)} />
              </td>
              <td className="block break-all px-4 py-1 font-mono text-xs text-muted-foreground md:table-cell md:break-normal md:py-3">
                <span className={cn(label, "font-sans")}>Key</span>
                {k.masked}
              </td>
              <td className="block px-4 py-1 text-muted-foreground md:table-cell md:py-3">
                <span className={label}>Last used</span>
                {k.last_used_at
                  ? new Date(k.last_used_at).toLocaleDateString()
                  : "Never"}
              </td>
              {/* The date the server stops taking it (`expires_at`, null for
                  a key made to never expire): the list said only "Expired"
                  once it was too late (functional test of 28 Oct 2026). */}
              <td className="block px-4 py-1 text-muted-foreground md:table-cell md:whitespace-nowrap md:py-3">
                <span className={label}>Expires</span>
                {k.expires_at ? new Date(k.expires_at).toLocaleDateString() : "Never"}
                {/* Within the notice the webhooks get, said before the day
                    (lib/keyLife.ts, endNear). */}
                <EndBadge near={endNear(k, warningDays, now)} />
              </td>
              <td className="block px-4 py-1.5 md:table-cell md:py-3">
                {!k.revoked && (
                  <div className="flex flex-wrap items-center gap-1 md:justify-end">
                    <MoveKey
                      currentProjectId={k.project_id}
                      projects={moveTargets}
                      onMove={(projectId) => onMove(k.id, projectId, k.name ?? k.masked ?? "this key")}
                    />
                    {/* Where the server takes a rotation (keys.rotates): not a
                        connected app's key, which renews its own secret. */}
                    {k.rotates && (
                      <button
                        type="button"
                        onClick={() => onRotate(k)}
                        className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        Rotate
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onRevoke(k.id)}
                      className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Revoke
                    </button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A compact select that re-points a key to a different project on change. */
function MoveKey({
  currentProjectId,
  projects,
  onMove,
}: {
  currentProjectId: string | null;
  projects: AgentProject[];
  onMove: (projectId: string) => void;
}) {
  // The backend models "Default" as a real project row, so it appears in the
  // list. Resolve the current value to whichever project this key sits in.
  const current =
    projects.find((p) => p.id === currentProjectId) ??
    projects.find((p) => p.is_default);
  if (projects.length <= 1) return null;
  return (
    <select
      aria-label="Move to project"
      value={current?.id ?? ""}
      onChange={(e) => {
        const next = e.target.value;
        if (next && next !== current?.id) onMove(next);
      }}
      className="max-w-48 rounded border border-input bg-background px-2 py-1 text-xs text-muted-foreground outline-hidden focus:ring-2 focus:ring-ring"
    >
      {projects.map((p) => (
        <option key={p.id} value={p.id}>
          {p.is_default ? `${p.name} (default)` : p.name}
        </option>
      ))}
    </select>
  );
}


function CreateKeyDialog({
  projects,
  defaultProjectId,
  lifetimes,
  onClose,
  onCreated,
}: {
  projects: AgentProject[];
  defaultProjectId: string | null;
  lifetimes: Load<KeyLifetimes>;
  onClose: () => void;
  onCreated: (k: AgentKeyCreated) => void;
}) {
  const [name, setName] = useState("");
  // Default the select to the project picked in the switcher (if it's a live
  // target), else fall back to the account's Default project.
  const initialProjectId =
    projects.find((p) => p.id === defaultProjectId && !p.archived)?.id ??
    projects.find((p) => p.is_default)?.id ??
    "";
  const [projectId, setProjectId] = useState<string>(initialProjectId);
  // What the install offers, and what it defaults to. Read rather than
  // hardcoded, so the two cannot drift. Until it is read, or when it cannot
  // be, `expiresIn` is undefined: the field is not sent and the key gets the
  // install's default lifetime, which the dialog then says. It used to fall
  // back to a list written here and to 90 days, silently (audit of 17 Oct
  // 2026). The page reads them once (keysApi.lifetimes) for the dialog, the
  // rows' notice and Rotate.
  const [expiresIn, setExpiresIn] = useState<number | null | undefined>(undefined);
  useEffect(() => {
    if (lifetimes.data) setExpiresIn(lifetimes.data.default_days);
  }, [lifetimes.data]);
  // What the key may do (GET /admin/scopes): the default set ticked, each
  // scope with the server's own words for what it opens. Unread, `scopes`
  // stays null and the key gets the install's default set.
  const scopeRead = useLoad(keysApi.scopes, [], "The scopes could not be read.");
  const scopeList = scopeRead.data?.scopes ?? [];
  const [scopes, setScopes] = useState<string[] | null>(null);
  useEffect(() => {
    if (scopeRead.data) setScopes(scopeRead.data.default);
  }, [scopeRead.data]);
  const toggleScope = (name: string) =>
    setScopes((cur) =>
      (cur ?? []).includes(name) ? (cur ?? []).filter((x) => x !== name) : [...(cur ?? []), name],
    );
  // Confined to some end users of the project (services/user_scope.py):
  // empty is the whole project.
  const [users, setUsers] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const targets = projects.filter((p) => !p.archived);
  // What stops a create, for the button and for Enter in the name field
  // alike. Enter called `submit` past the disabled button: twice quickly was
  // two POSTs (the second a 409), and with nothing ticked it minted a key
  // with no scopes (audit of 17 Oct 2026).
  const blocked = busy || (scopes !== null && scopes.length === 0);

  const submit = async () => {
    if (blocked) return;
    setErr(null);
    setBusy(true);
    try {
      // Always send the project that is on screen, including Default.
      //
      // This used to omit it for Default, on the grounds that the backend reads
      // null as Default and the request stayed minimal. That was true while a
      // project was a label. It is not true now that it is a wall: null means
      // "resolve at read time", so a key created that way follows whichever
      // project is default later, instead of staying where it was put.
      //
      // It also read as broken. A tester created keys with Default on screen,
      // saw project_id: null come back four times, and concluded the dialog did
      // not send the field. The dialog was doing what it said in a comment and
      // not what it showed on screen, which is the same thing as being wrong.
      const chosen = targets.find((p) => p.id === projectId);
      const projectArg = chosen ? chosen.id : undefined;
      onCreated(
        await keysApi.create(
          name.trim() || undefined,
          projectArg,
          expiresIn,
          scopes ?? undefined,
          endUsersFrom(users),
        ),
      );
    } catch (e) {
      // The server's own words when it refused the list (a 422 says why).
      setErr(errorMessage(e) ?? "Could not create the key. Please try again.");
      setBusy(false);
    }
  };

  return (
    <Modal title="Create API key" onClose={onClose}>
      <label className="mb-1.5 block text-sm font-medium">Name (optional)</label>
      <input
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && submit()}
        placeholder="e.g. production support bot"
        className={`mb-1 w-full ${fieldClass}`}
      />
      {/* Under the field it describes: it sat after the Project select,
          which it does not describe (QA of 14 Nov 2026). */}
      <p className="mb-3 text-xs text-muted-foreground">
        A label to recognize this key later. Make one key per agent or machine and name it
        after it: a key works from anywhere it is copied, so one that leaks is then
        rotated or revoked alone. You can revoke it any time.
      </p>

      <label className="mb-1.5 block text-sm font-medium">Expires</label>
      {lifetimes.error ? (
        <LoadNote load={lifetimes} className="mb-3">
          The key gets this install's default lifetime.
        </LoadNote>
      ) : (
        <>
          <select
            aria-label="Key lifetime"
            disabled={!lifetimes.data}
            value={expiresIn === null ? "never" : String(expiresIn ?? "")}
            onChange={(e) =>
              setExpiresIn(e.target.value === "never" ? null : Number(e.target.value))
            }
            className={`mb-1 w-full ${fieldClass}`}
          >
            {lifetimes.data ? (
              lifetimes.data.choices.map((c) => (
                <option key={c.label} value={c.days === null ? "never" : String(c.days)}>
                  {c.label}
                </option>
              ))
            ) : (
              <option value="">Loading…</option>
            )}
          </select>
          <p className="mb-3 text-xs text-muted-foreground">
            {expiresIn === null
              ? "It will keep working until somebody revokes it."
              : lifetimes.data
                ? `Your webhooks get ${lifetimes.data.warning_days} days' notice before it stops.`
                : "\u00a0"}
          </p>
        </>
      )}

      {targets.length > 0 && (
        <>
          <label className="mb-1.5 block text-sm font-medium">Project</label>
          <select
            aria-label="Project for this key"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className={`mb-1 w-full ${fieldClass}`}
          >
            {targets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.is_default ? `${p.name} (default)` : p.name}
              </option>
            ))}
          </select>
          <p className="mb-4 text-xs text-muted-foreground">
            The project whose memories the key writes and reads.
          </p>
        </>
      )}
      {scopeRead.error && (
        <LoadNote load={scopeRead} className="mb-4">
          The key gets this install's default scopes.
        </LoadNote>
      )}
      {scopeList.length > 0 && scopes && (
        <fieldset className="mb-4">
          <legend className="mb-1.5 block text-sm font-medium">What it may do</legend>
          <div className="space-y-1.5">
            {scopeList.map((sc) => (
              <label key={sc.name} className="flex cursor-pointer items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={scopes.includes(sc.name)}
                  onChange={() => toggleScope(sc.name)}
                  className="mt-0.5 h-4 w-4 rounded border-input"
                />
                <span>
                  <span className="font-mono text-xs">{sc.name}</span>
                  <span className="block text-xs text-muted-foreground">{sc.opens}</span>
                </span>
              </label>
            ))}
          </div>
          {scopes.length === 0 && (
            <p className="mt-1.5 text-xs text-destructive">Tick at least one.</p>
          )}
        </fieldset>
      )}
      <label htmlFor="key-users" className="mb-1.5 block text-sm font-medium">
        Only these end users (optional)
      </label>
      <input
        id="key-users"
        value={users}
        onChange={(e) => setUsers(e.target.value)}
        placeholder="e.g. founder, repo:*"
        className="mb-1 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-hidden focus:ring-2 focus:ring-ring"
      />
      <p className="mb-4 text-xs text-muted-foreground">
        Empty: the whole project. Otherwise the key reads and writes only these user_ids; a
        trailing * is a prefix (repo:* is every repository). It cannot have admin.
      </p>
      <ModalActions
        error={err}
        onCancel={onClose}
        confirmLabel={busy ? "Creating…" : "Create key"}
        onConfirm={submit}
        confirmDisabled={blocked}
      />
    </Modal>
  );
}

/**
 * Rotate: a new secret for the same key (its name, project, scopes and end
 * users), POST /admin/keys/{id}/rotate, with the old secret working a while
 * longer so the agents that hold it can take the new one, or ending now for
 * a key that leaked. Stripe's "Rotate key" asks the same question, with
 * "Now" ending the old key at once (https://docs.stripe.com/keys, read 15
 * Nov 2026). The choices are the server's (lib/keyLife.ts, overlapChoices).
 */
function RotateKeyDialog({
  target,
  lifetimes,
  onClose,
  onRotated,
  onFailed,
}: {
  target: AgentKey;
  lifetimes: KeyLifetimes | null;
  onClose: () => void;
  onRotated: (k: AgentKeyCreated) => void;
  /** The list read again: a refusal can mean it is out of date (a key
   *  that expired meanwhile), and a rotation with no answer may have
   *  changed the key. */
  onFailed: () => void;
}) {
  const offer = overlapChoices(lifetimes);
  const [picked, setPicked] = useState<number | undefined | null>(null);
  const hours = overlapInForce(offer, picked);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // A rotation that went through with no answer, its old secret still
  // working: one more from here would end the secret the agents hold.
  const [stopped, setStopped] = useState(false);
  // One rotation at a time (lib/keyLife.ts): `busy` is the button's, and a
  // second click can come before it is drawn.
  const held = useRef(false);

  const submit = async () => {
    setErr(null);
    setBusy(true);
    await rotateFromTheScreen(keysApi, target, hours, held, {
      shown: onRotated,
      failed: (text, again) => {
        setErr(text);
        setBusy(false);
        setStopped(!again);
        onFailed();
      },
    });
  };

  // `busy`: while the call is out, Escape, the backdrop and Cancel do not
  // close the dialog (Modal.tsx), as on every dialog that sends one. Closed,
  // the rotation went on, its secret appeared after the Cancel and a refusal
  // was never seen; reopened, the dialog had a new `held`, and a second
  // rotation went out beside the first (the skeptic's finding of 15 Nov
  // 2026).
  return (
    <Modal title="Rotate API key" onClose={onClose} busy={busy}>
      <p className="mb-3 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">{target.name || target.masked}</span> gets
        a new secret, shown once. Its name, project, scopes and end users stay; the new secret
        gets the lifetime this one was given, never more than a new key may last.
      </p>
      <OldSecretChoice offer={offer} picked={picked} onPick={setPicked} />
      <ModalActions
        error={err}
        onCancel={onClose}
        cancelDisabled={busy}
        confirmLabel={busy ? "Rotating…" : "Rotate key"}
        onConfirm={submit}
        confirmDisabled={busy || stopped}
      />
    </Modal>
  );
}

function RevealKeyDialog({
  keyValue,
  rotated,
  onClose,
}: {
  keyValue: AgentKeyCreated;
  rotated: boolean;
  onClose: () => void;
}) {
  return (
    <Modal title={rotated ? "Save the new secret" : "Save your API key"} onClose={onClose}>
      <p className="mb-3 text-sm text-muted-foreground">
        This is the only time the full key is shown. Copy it now and store it
        securely. If you lose it, revoke it and create a new one.
      </p>
      {rotated && (
        <p className="mb-3 text-sm text-muted-foreground">{afterRotation(keyValue, Date.now())}</p>
      )}
      {keyValue.project_name && (
        <p className="mb-3 text-xs text-muted-foreground">
          Scoped to project{" "}
          <span className="font-medium text-foreground">
            {keyValue.project_name}
          </span>
          .
        </p>
      )}
      <div className="mb-4 flex items-center gap-2 rounded-md border border-border bg-secondary/50 px-3 py-2">
        <code className="flex-1 break-all font-mono text-xs">{keyValue.key}</code>
        <CopyButton
          text={keyValue.key}
          label="Copy key"
          iconClassName="h-4 w-4"
          className="shrink-0 rounded p-1.5 text-muted-foreground hover:bg-background hover:text-foreground"
        />
      </div>
      <div className="flex justify-end">
        <button
          type="button"
          onClick={onClose}
          className="rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          Done
        </button>
      </div>
    </Modal>
  );
}
