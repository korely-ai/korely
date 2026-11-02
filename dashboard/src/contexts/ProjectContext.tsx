import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { projectsApi, type AgentProject } from "@/lib/agentsApi";
import { useLoad, type Load } from "@/lib/useLoad";

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
const STORAGE_KEY = "korely_agents_active_project";

interface ProjectContextValue {
  /** The list as read, or empty while it is not: never draw it as "none". */
  projects: AgentProject[];
  /** The read itself: what a view that shows the list draws. */
  projectsLoad: Load<AgentProject[]>;
  activeProject: AgentProject | null;
  activeProjectId: string | null;
  setActiveProjectId: (id: string) => void;
  /** Read the list again, keeping the one on screen until it answers. */
  refresh: () => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const projectsLoad = useLoad(
    () => projectsApi.list().then((r) => r.projects),
    [],
    "Could not load your projects.",
  );
  const list = projectsLoad.data;
  const projects = useMemo(() => list ?? [], [list]);
  const [activeProjectId, setActiveIdState] = useState<string | null>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  });

  // Resolve the active selection against each fresh list: keep it if it's
  // still a live project, otherwise fall back to Default (then first).
  useEffect(() => {
    if (!list) return;
    setActiveIdState((cur) => {
      if (cur && list.some((p) => p.id === cur && !p.archived)) return cur;
      const fallback = list.find((p) => p.is_default) ?? list[0] ?? null;
      return fallback ? fallback.id : null;
    });
  }, [list]);

  // Persist the selection whenever it changes.
  useEffect(() => {
    try {
      if (activeProjectId) localStorage.setItem(STORAGE_KEY, activeProjectId);
    } catch {
      /* storage unavailable — selection stays in-memory for the session */
    }
  }, [activeProjectId]);

  const setActiveProjectId = useCallback((id: string) => setActiveIdState(id), []);

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
      refresh,
    }),
    [projects, projectsLoad, activeProject, activeProjectId, setActiveProjectId, refresh],
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProjects() {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProjects must be used within a ProjectProvider");
  return ctx;
}
