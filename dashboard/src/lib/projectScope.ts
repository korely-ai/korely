// Which project a read of the screen covers, and the switcher's choice
// checked against the list of projects: what the data pages read
// (contexts/ProjectContext.tsx). Here, without React, so that a check run
// with node executes it (scripts/check_the_reads.mjs).
import type { AgentProject } from "@/lib/agentsApi";

/** Not exported: no other module can name it, so none can make a scope. */
declare const madeHere: unique symbol;

/**
 * One project of the account, or every project, said as such: never by
 * leaving the project out. The server reads a request without `project_id`
 * as the whole account (api/admin.py PROJECT_FILTER), so a read that forgot
 * the project and a read meant for the whole account were the same request:
 * with a project of 9 facts chosen at the top the Memory Graph drew the 2,808
 * facts of the account (6 Nov 2026). Every read of data that lives in a
 * project takes one of these, required, as the server's own EVERY_PROJECT
 * (services/sentinels.py) is deliberately not spelled None.
 *
 * Only this module makes one (`EVERY_PROJECT`, `scopeOf`, `resolveScope`):
 * the mark it carries is a symbol no other module can name, so a page that
 * writes `{ kind: "every" }` by hand does not type-check (a review's
 * mutations, 6 Nov 2026: a page could read the account under a project's name). The
 * pages get theirs from the switcher (`useProjectScope`).
 */
export type ProjectScope = (
  | { readonly kind: "project"; readonly id: string; readonly name: string }
  | { readonly kind: "every" }
) & { readonly [madeHere]: true };

/** Every project of the account, chosen as such ("All projects"). */
export const EVERY_PROJECT = { kind: "every" } as ProjectScope;

/** One project of the account. */
export function scopeOf(project: Pick<AgentProject, "id" | "name">): ProjectScope {
  return { kind: "project", id: project.id, name: project.name } as ProjectScope;
}

/** The scope as the query parameter every /admin read takes: `project_id`,
 *  or none for every project of the account. */
export function inScope(scope: ProjectScope): { project_id?: string } {
  return scope.kind === "project" ? { project_id: scope.id } : {};
}

/** What the switcher stores for "All projects": never a project's id. */
export const EVERY = "every";

/** Where this browser keeps one account's choice. One place for every
 *  account kept "All projects", or a project of another account, from one
 *  sign-in to the next in the same browser (review of 6 Nov 2026). */
export function choiceKey(account: string): string {
  return `korely_agents_active_project:${account}`;
}

/** The choice, checked against the list: "All projects" as chosen; a live
 *  project as chosen; otherwise the Default project, then the first live one
 *  (a project deleted or archived elsewhere); with no project at all, the
 *  whole account, which is then all there is. Null until the list is read,
 *  and while it is read again for a choice it does not hold yet (a project
 *  just made in the switcher): the Default project's pages were mounted and
 *  read for that moment. Never the whole account unless it was chosen or
 *  there is nothing else: a page reads nothing while this is null. */
export function resolveScope(
  list: AgentProject[] | null,
  chosen: string | null,
  rereading: boolean,
): ProjectScope | null {
  if (!list) return null;
  if (chosen === EVERY) return EVERY_PROJECT;
  const live = list.filter((p) => !p.archived);
  const picked = live.find((p) => p.id === chosen);
  if (!picked && chosen && rereading) return null;
  const project = picked ?? live.find((p) => p.is_default) ?? live[0] ?? null;
  return project ? scopeOf(project) : EVERY_PROJECT;
}

/** One string per scope: the project's id, or "every". */
export function scopeKeyOf(scope: ProjectScope): string {
  return scope.kind === "project" ? scope.id : EVERY;
}
