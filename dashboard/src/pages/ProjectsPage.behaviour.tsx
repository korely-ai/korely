// What a row of the Projects page offers, and what its Archive and Unarchive
// send (ProjectsPage.tsx, ProjectActions; lib/agentsApi.ts projectsApi): run
// in node by scripts/check_the_behaviour.mjs (`npm test`). Up to 1.0.38 the
// screen read only the live projects and had no Unarchive: a project
// archived from it was gone from it, and only an admin key brought it back.
import { isValidElement, type ReactElement, type ReactNode } from "react";
import type { AxiosResponse, InternalAxiosRequestConfig } from "axios";
import { renderToString } from "react-dom/server";
import apiClient from "@/lib/apiClient";
import { projectsApi, type AgentProject } from "@/lib/agentsApi";
import { fakeBrowser, inBrowser, type Check } from "@/lib/behaviour";
import { liveOnly } from "@/lib/projectScope";
import type { Load } from "@/lib/useLoad";
import { ModeContext } from "@/contexts/ModeContext";
import { ProjectsFrom, useProjectName, useProjects } from "@/contexts/ProjectContext";
import { ProjectActions, ProjectsPage, archiveQuestion, type RowPage } from "@/pages/ProjectsPage";

const project = (over: Partial<AgentProject>): AgentProject => ({
  id: "p-1",
  name: "Staging",
  slug: "staging",
  is_default: false,
  archived: false,
  key_count: 0,
  description: null,
  region: null,
  processing_region: "eu",
  created_at: "2026-11-25T08:00:00Z",
  ...over,
});
const LIVE = project({});
const ARCHIVED = project({ id: "p-2", name: "Old shop", slug: "old-shop", archived: true });
const DEFAULT = project({ id: "p-0", name: "Default", slug: "default", is_default: true });

type Done = string[];
/** What a page hands a row, recording what the row does with it: the route
 *  each send carries, by its function. */
const handlers = (done: Done, yes = true): RowPage => ({
  edit: (p) => void done.push(`edit ${p.id}`),
  remove: (p) => void done.push(`remove ${p.id}`),
  ask: (q) => (done.push(`ask ${q}`), yes),
  send: (p, call) => void done.push(`send ${p.id} ${
    call === projectsApi.archive ? "archive" : call === projectsApi.unarchive ? "unarchive" : "other"}`),
});

/** The titles of the buttons a row draws. */
const offered = (p: AgentProject) =>
  [...renderToString(<ProjectActions project={p} busy={false} page={handlers([])} />)
    .matchAll(/<button[^>]*title="([^"]+)"/g)].map((m) => m[1]);

/** Press the row's button titled `title`, as a click calls it. */
function press(p: AgentProject, title: string, busy = false, yes = true): { done: Done; disabled: boolean } {
  const done: Done = [];
  let found: ReactElement<{ title?: string; onClick?: () => void; disabled?: boolean }> | null = null;
  const walk = (node: ReactNode) => {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!isValidElement(node)) return;
    const el = node as ReactElement<{ title?: string; children?: ReactNode; onClick?: () => void; disabled?: boolean }>;
    if (el.type === "button" && el.props.title === title) found = el;
    walk(el.props.children);
  };
  walk(ProjectActions({ project: p, busy, page: handlers(done, yes) }));
  if (!found) throw new Error(`no button "${title}" for ${p.name}`);
  const button = found as ReactElement<{ onClick?: () => void; disabled?: boolean }>;
  button.props.onClick?.();
  return { done, disabled: Boolean(button.props.disabled) };
}

/** What the real client sends for `call`, answered 200. */
async function sent(call: () => Promise<unknown>): Promise<string[]> {
  const seen: string[] = [];
  const before = apiClient.defaults.adapter;
  apiClient.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    const url = new URL(apiClient.getUri(config), "http://screen.test");
    seen.push(`${String(config.method).toUpperCase()} ${url.pathname}${url.search}`);
    const data = { projects: [], total: 0 };
    return { data, status: 200, statusText: "OK", headers: {}, config } as AxiosResponse;
  };
  try {
    await inBrowser(fakeBrowser(), call);
  } finally {
    apiClient.defaults.adapter = before;
  }
  return seen;
}

/** The projects' read, answered with `list`. */
const answered = (list: AgentProject[]): Load<AgentProject[]> => ({
  data: list, error: null, loading: false, reload: () => {}, refused: false, waitUntil: null,
});
const LIST = [DEFAULT, LIVE, ARCHIVED];

/** What a view under the app's provider is given, the provider handed LIST. */
async function given() {
  let seen: { projects: string[]; withArchived: string[]; name: string | null } | null = null;
  function Probe() {
    const { projects, withArchived } = useProjects();
    const nameOf = useProjectName();
    seen = { projects: projects.map((p) => p.id), withArchived: withArchived.map((p) => p.id),
             name: nameOf(ARCHIVED.id) };
    return null;
  }
  await inBrowser(fakeBrowser(), () =>
    renderToString(<ProjectsFrom account="acc-1" projectsLoad={answered(LIST)}><Probe /></ProjectsFrom>));
  return seen!;
}

/** The Projects page as the app draws it, from a read of LIST. */
const page = () =>
  inBrowser(fakeBrowser(), () =>
    renderToString(
      <ModeContext.Provider value={{ hosted: false, config: null, keyPrefix: "", mcpPath: "" }}>
        <ProjectsFrom account="acc-1" projectsLoad={answered(LIST)}><ProjectsPage /></ProjectsFrom>
      </ModeContext.Provider>,
    ).replace(/<!-- -->/g, ""));

export const CHECKS: Record<string, Check> = {
  "the provider gives the switcher the live projects and the Projects page all of them": async (t) => {
    const seen = await given();
    t.deepEqual(seen.projects, ["p-0", "p-1"], "projects: the live ones");
    t.deepEqual(seen.withArchived, ["p-0", "p-1", "p-2"], "withArchived: all");
    t.equal(seen.name, "Old shop", "a name is read among the archived ones too");
  },
  "the Projects page draws the archived project, with Unarchive": async (t) => {
    const out = await page();
    t.ok(out.includes("Old shop") && out.includes("Archived"), out);
    t.ok(out.includes('title="Unarchive"'), "its button is drawn");
  },

  "an archived project offers Unarchive and no Archive; a live one the other way round": (t) => {
    t.deepEqual(offered(ARCHIVED), ["Edit", "Unarchive", "Delete"]);
    t.deepEqual(offered(LIVE), ["Edit", "Archive", "Delete"]);
    t.deepEqual(offered(DEFAULT), ["Edit"], "the Default project is never archived");
  },
  "a row's Unarchive sends the unarchive and asks nothing; Archive asks, then archives": (t) => {
    t.deepEqual(press(ARCHIVED, "Unarchive").done, ["send p-2 unarchive"]);
    t.deepEqual(press(LIVE, "Archive").done, [`ask ${archiveQuestion(LIVE)}`, "send p-1 archive"]);
    t.deepEqual(press(LIVE, "Archive", false, false).done, [`ask ${archiveQuestion(LIVE)}`],
      "a no sends nothing");
    t.deepEqual(press(LIVE, "Edit").done, ["edit p-1"]);
    t.deepEqual(press(LIVE, "Delete").done, ["remove p-1"]);
    t.equal(press(ARCHIVED, "Unarchive", true).disabled, true, "held while the row is busy");
  },
  "Unarchive sends the route that brings the project back": async (t) => {
    t.deepEqual(await sent(() => projectsApi.unarchive("p-2")), ["POST /admin/projects/p-2/unarchive"]);
    t.deepEqual(await sent(() => projectsApi.archive("p-1")), ["POST /admin/projects/p-1/archive"]);
  },
  "the screen's list asks for the archived ones too; the switcher keeps the live ones": async (t) => {
    t.deepEqual(await sent(() => projectsApi.list({ archived: true })),
      ["GET /admin/projects?include_archived=true"]);
    t.deepEqual(await sent(() => projectsApi.list()), ["GET /admin/projects"]);
    t.deepEqual(liveOnly([DEFAULT, LIVE, ARCHIVED]).map((p) => p.id), ["p-0", "p-1"]);
  },
  "Archive says the way back before it is sent": (t) => {
    const q = archiveQuestion(LIVE);
    t.ok(q.includes('"Staging"') && q.includes("Unarchive"), q);
  },
};
