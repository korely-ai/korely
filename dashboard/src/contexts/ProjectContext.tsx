import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Outlet } from "react-router";
import { projectsApi, type AgentProject, type ProjectScope } from "@/lib/agentsApi";
import { EVERY, choiceKey, resolveScope, scopeKeyOf } from "@/lib/projectScope";
import { useLoad, type Load } from "@/lib/useLoad";
import { useAuth } from "@/contexts/AuthContext";
import { LoadNote } from "@/components/LoadState";

// The active project lives here, app-wide, so the top-left switcher and any
// project-scoped view share one source of truth. Persisted per browser so a
// reload lands you back in the project you were working in. The list too: the
// Projects page kept a copy of its own until the audit of 17 Oct 2026, and a
// rename, an archive or a delete there left the switcher on the old list
// until a reload (it still offered a deleted project). Whatever changes a
// project calls `refresh` here, and every view redraws from the same list.
//
// The list is a lib/useLoad.ts read. It was read by hand into a list that
// started empty, with a `loadError` flag only the Projects page looked at:
// a failed read was an empty switcher ("No projects found.") everywhere else
// (audit of 17 Oct 2026). A view that draws the list draws `projectsLoad`
// (components/LoadState.tsx); `projects` is for looking one up.
//
// What the switcher chooses is also what the data pages read (`scope`, 6 Nov
// 2026): one project, or "All projects", the whole account, chosen as such.
// The choice was a label until then: the Memory Graph, Memories, Facts,
// Agents, Requests, the Audit log and the Home page's numbers read every
// project whatever it said, and with a project of 9 facts chosen the
// graph drew the account's 2,808.
//
// The choice is kept per account (lib/projectScope.ts choiceKey): one key for
// the whole browser kept "All projects" from one account's sign-in to the
// next account's (review of 6 Nov 2026). The one key of that version is removed.
const OLD_STORAGE_KEY = "korely_agents_active_project";

interface ProjectContextValue {
  /** The list as read, or empty while it is not: never draw it as "none". */
  projects: AgentProject[];
  /** The read itself: what a view that shows the list draws. */
  projectsLoad: Load<AgentProject[]>;
  /** The project chosen at the top; null for "All projects" and while the
   *  list is being read. */
  activeProject: AgentProject | null;
  activeProjectId: string | null;
  setActiveProjectId: (id: string) => void;
  /** Choose "All projects": the data pages read the whole account. */
  chooseEveryProject: () => void;
  /** What the data pages read: the chosen project or every project; null
   *  until the choice is checked against the list (or the list failed), so
   *  that no page reads the whole account meanwhile. */
  scope: ProjectScope | null;
  /** Read the list again, keeping the one on screen until it answers. */
  refresh: () => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

/** The projects of the account signed in. Mounted again for another
 *  account (a sign-in, or the Cloud's claim that adopts one): its list and
 *  its choice are read again, never carried over. */
export function ProjectProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const account = user?.uid ?? "";
  return (
    <AccountProjects key={account} account={account}>
      {children}
    </AccountProjects>
  );
}

function AccountProjects({ account, children }: { account: string; children: ReactNode }) {
  const projectsLoad = useLoad(
    () => projectsApi.list().then((r) => r.projects),
    [],
    "Could not load your projects.",
  );
  const list = projectsLoad.data;
  const projects = useMemo(() => list ?? [], [list]);
  const [chosen, setChosen] = useState<string | null>(() => {
    try {
      localStorage.removeItem(OLD_STORAGE_KEY);
      return localStorage.getItem(choiceKey(account));
    } catch {
      return null;
    }
  });

  // Resolved in the render, not by an effect after it: an effect left one
  // render with the stored choice unresolved, and a page mounted in it would
  // read a project deleted since, or the whole account.
  const resolved = resolveScope(list, chosen, projectsLoad.loading);
  const scopeKey = resolved ? scopeKeyOf(resolved) : null;
  const scopeName = resolved?.kind === "project" ? resolved.name : null;
  // One object per project and name: a read of the list again that keeps the
  // choice gives the pages the same scope, and they do not read again.
  const scope = useMemo(() => resolved, [scopeKey, scopeName]);

  // Persist what is shown, so a reload lands on it.
  useEffect(() => {
    if (!scopeKey) return;
    try {
      localStorage.setItem(choiceKey(account), scopeKey);
    } catch {
      /* storage unavailable — the choice stays in memory for the session */
    }
  }, [account, scopeKey]);

  const setActiveProjectId = useCallback((id: string) => setChosen(id), []);
  const chooseEveryProject = useCallback(() => setChosen(EVERY), []);

  const activeProjectId = scope?.kind === "project" ? scope.id : null;
  const activeProject = useMemo(
    () => projects.find((p) => p.id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );

  const refresh = projectsLoad.reload;
  const value = useMemo(
    () => ({
      projects,
      projectsLoad,
      activeProject,
      activeProjectId,
      setActiveProjectId,
      chooseEveryProject,
      scope,
      refresh,
    }),
    [projects, projectsLoad, activeProject, activeProjectId, setActiveProjectId,
     chooseEveryProject, scope, refresh],
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProjects() {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProjects must be used within a ProjectProvider");
  return ctx;
}

/** A project's name by its id, from the account's list the switcher read;
 *  null while it is not read, or for an id not in it. For a page of the
 *  data that names the project of a row it shows from another project,
 *  never for what it reads: that is the scope (useProjectScope). */
export function useProjectName(): (id: string) => string | null {
  const { projects } = useProjects();
  return (id) => projects.find((p) => p.id === id)?.name ?? null;
}

/**
 * The data pages, under the project chosen at the top (App.tsx wraps their
 * routes in this). Nothing of them renders until the choice is checked
 * against the list, and a list that could not be read is said, with a retry.
 * A change of project mounts the page again: every read it makes starts over
 * in the new project, its filters, pages and selection with them, so no page
 * can keep showing the project left (one way for every page, not a list of
 * dependencies each page has to remember).
 */
export function InProjectScope() {
  const { scope, projectsLoad } = useProjects();
  if (!scope) {
    return projectsLoad.error ? (
      <LoadNote load={projectsLoad} />
    ) : (
      <p className="text-sm text-muted-foreground">Loading…</p>
    );
  }
  return <Outlet key={scopeKeyOf(scope)} />;
}

/** The scope a data page reads in. Only under InProjectScope, which renders
 *  nothing until there is one. */
export function useProjectScope(): ProjectScope {
  const { scope } = useProjects();
  if (!scope) throw new Error("useProjectScope must be used under InProjectScope");
  return scope;
}
