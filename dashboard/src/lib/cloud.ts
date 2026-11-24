// The Cloud's pieces of the screen (dashboard/src/hosted/), when this tree
// carries them: the screen's side of korely_agent/core/extensions.py. The
// public repository has no hosted/ folder (the decision of 28 Oct 2026):
// there `cloud` is null, `import.meta.glob` finds nothing to bundle,
// and the screen is the one of an install of one's own. Only a server that
// says it is the hosted service, and a build that has the pieces, switch them
// on (contexts/ModeContext.tsx).
import { useEffect, useSyncExternalStore, type ComponentType, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import type { ReturnAt } from "./returns";

/** What GET /hosted/config answers, read only by the Cloud's own pieces. */
export type CloudConfig = object;

export interface CloudScreen {
  /** GET /hosted/config, asked only once /health says `"mode": "hosted"`. */
  loadConfig(): Promise<CloudConfig | null>;
  /** The address the Install page's snippets carry. */
  apiBase(config: CloudConfig | null, fallback: string): string;
  /** The sign-in provider, the login, the plan and the account: lazy
   *  chunks, never downloaded by the screen of an install of one's own. */
  AuthProvider: ComponentType<{ children: ReactNode }>;
  LoginPage: ComponentType;
  PlanPage: ComponentType;
  AccountPage: ComponentType;
  /** This month's use against the plan, in the sidebar. */
  SidebarUsage: ComponentType;
  /** The named agents each plan holds, on the Agents page. */
  AgentCaps: ComponentType;
  /** This month's writes, for the home page's card. */
  useWritesThisMonth(): { value: string; sub?: string };
  /** The parts of the product the server serves the signed-in account
   *  (GET /hosted/account `parts`); none until it is known. */
  useServedParts(): readonly string[];
  /** The screen's parts (`ScreenPart`), a lazy chunk of their own: asked
   *  once the server names a part for the signed-in account
   *  (`useScreenParts`), and at the start on an address of the shape of a
   *  source's return (`partsChunk`, lib/returns.ts), whoever opens it. */
  loadParts(): Promise<readonly ScreenPart[]>;
}

const found = import.meta.glob<CloudScreen>("../hosted/index.ts", {
  eager: true,
  import: "screen",
});

export const cloud: CloudScreen | null = found["../hosted/index.ts"] ?? null;

/** The parts' chunk of this build (`CloudScreen.loadParts`), null in a
 *  build without the Cloud's pieces: what the start (main.tsx,
 *  lib/returns.ts) and `useScreenParts` ask. */
export const partsChunk: (() => Promise<readonly ScreenPart[]>) | null = cloud ? () => cloud!.loadParts() : null;

/** A part of the product the Cloud's tree alone carries (the founder's
 *  decision of 4 Nov 2026: korely_agent/core/extensions.py's `Part`, on the
 *  screen): a page, its route and its entry in the sidebar's Activity,
 *  after Memories. Shown only by the hosted service's screen, as the server
 *  serves a part's routes only in its mode (core/extensions.mounted), and
 *  only to an account the server serves it (core/extensions.served,
 *  `partsFor`; 13 Nov 2026), from a chunk of its own (`loadParts`). Its
 *  page is a page of the data: App.tsx mounts it under InProjectScope, and it
 *  reads the project chosen at the top (`useProjectScope`), as the core's
 *  pages of the data do (6 Nov 2026). */
export interface ScreenPart {
  /** The part of the product on the server this is the screen of
   *  (core/extensions.Part.name): shown only to an account the server says
   *  it serves (`partsFor`). */
  part: string;
  path: string;
  label: string;
  icon: LucideIcon;
  /** A lazy chunk, downloaded when the page is opened. */
  Page: ComponentType;
  /** The addresses under `path` a source outside sends a person back to
   *  (an OAuth consent's return address, `<source>/callback`: every one
   *  ends in `/callback`, the shape the main chunk knows), with values that
   *  must not stay in the address bar: taken out at the start, before
   *  anything renders, and handed to the page once (lib/returns.ts). No
   *  other address under `path` is routed. */
  returns?: readonly ReturnAt[];
  /** Its lines on the plan page (/usage), drawn from that page's answer
   *  (GET /hosted/usage), which carries the part's own numbers only for an
   *  account the server serves it: a part its plan caps says what it uses
   *  there, in its own words, from its own chunk. */
  Usage?: ComponentType<{ usage: object }>;
  /** What it adds to the Playground's search (pages/PlaygroundPage.tsx,
   *  lib/playground.ts), for an account the server serves it. */
  search?: PartSearch;
}

/** A part's kind of result in the Playground's one search (GET
 *  /admin/memories/search with `types`, as POST /v1/memories/search takes
 *  it): asked beside the memories, merged with them by rank as an agent's
 *  search that names the kind gets them, and drawn by the part, in its own
 *  words, from its own chunk. */
export interface PartSearch {
  /** The kind the search is asked for, as `types` names it. */
  kind: string;
  /** The `type` its results say. */
  type: string;
  /** What the page calls them, in the plural. */
  label: string;
  /** One of its results, as the search answered it. */
  Found: ComponentType<{ hit: object }>;
}

/** Of `parts`, the screen's parts, those of `served`, the parts the server
 *  serves the signed-in account (its GET /hosted/account `parts`), in their
 *  order: the screen never decides for itself which account a part is for. */
export function partsFor(served: readonly string[], parts: readonly ScreenPart[]): ScreenPart[] {
  return parts.filter((p) => served.includes(p.part));
}

/** The parts the signed-in account is served, and whether the screen knows
 *  them yet: not while their chunk is on its way. An address no route has
 *  waits for `ready` before it is sent home (App.tsx), or the address of a
 *  part would be, before its route exists. */
export interface ScreenParts {
  parts: ScreenPart[];
  ready: boolean;
}

/** `ScreenParts` for the names the server said (`served`) and the parts'
 *  chunk, once it is in (`loaded`, null before). */
export function screenPartsNow(
  served: readonly string[],
  loaded: readonly ScreenPart[] | null,
): ScreenParts {
  if (served.length === 0) return { parts: [], ready: true };
  if (loaded === null) return { parts: [], ready: false };
  return { parts: partsFor(served, loaded), ready: true };
}

/** The parts' chunk, asked once, the first time `want` is told a name: a
 *  store the screen is drawn again from when it comes in. One that fails
 *  to load is none, so the screen is not left waiting. */
export function partsLoader(load: (() => Promise<readonly ScreenPart[]>) | null) {
  let loaded: readonly ScreenPart[] | null = null;
  let asked = false;
  const told = new Set<() => void>();
  return {
    now: (): readonly ScreenPart[] | null => loaded,
    listen(tell: () => void): () => void {
      told.add(tell);
      return () => told.delete(tell);
    },
    want(served: readonly string[]): void {
      if (asked || served.length === 0) return;
      asked = true;
      (load ? load() : Promise.resolve([] as readonly ScreenPart[]))
        .catch(() => [] as readonly ScreenPart[])
        .then((parts) => {
          loaded = parts;
          told.forEach((tell) => tell());
        });
    },
  };
}

const theParts = partsLoader(partsChunk);
const useServed: () => readonly string[] = cloud?.useServedParts ?? (() => []);

/** The screen's parts the signed-in account is served (`screenPartsNow`):
 *  its routes and its sidebar's entries. Their chunk is asked the first
 *  time the server names one. */
export function useScreenParts(): ScreenParts {
  const served = useServed();
  const loaded = useSyncExternalStore(theParts.listen, theParts.now, theParts.now);
  useEffect(() => theParts.want(served), [served]);
  return screenPartsNow(served, loaded);
}
