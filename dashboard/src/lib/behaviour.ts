// What the screen's behaviour checks share (13 Nov 2026): the `*.behaviour.ts`
// files beside the modules they run, which dashboard/scripts/
// check_the_behaviour.mjs (`npm test`) loads through Vite, as the build
// compiles them, and runs in node. Not imported by the app.
//
// The dashboard has no DOM in node and no test runner of its own: a module
// whose behaviour matters is written so that what it does is a function of
// what it is handed (a clock, the address, the server's routes), and these
// hand it stand-ins and look at what it did.
import { AxiosError, type AxiosResponse } from "axios";

/** What a check asserts with (node:assert/strict's, handed by the runner). */
export interface Assert {
  equal(actual: unknown, expected: unknown, message?: string): void;
  notEqual(actual: unknown, expected: unknown, message?: string): void;
  deepEqual(actual: unknown, expected: unknown, message?: string): void;
  ok(value: unknown, message?: string): void;
  match(value: string, re: RegExp, message?: string): void;
  doesNotMatch(value: string, re: RegExp, message?: string): void;
}

/** A behaviour check: each `*.behaviour.ts` exports `CHECKS`, by name. */
export type Check = (t: Assert) => void | Promise<void>;

/** Time that moves only when told: timers run in the order they are due. */
export class FakeClock {
  now = 0;
  private next = 1;
  private timers = new Map<number, { at: number; fn: () => void }>();

  set = (fn: () => void, ms: number): number => {
    const id = this.next++;
    this.timers.set(id, { at: this.now + ms, fn });
    return id;
  };

  clear = (id: number): void => {
    this.timers.delete(id);
  };

  /** Timers waiting. */
  get pending(): number {
    return this.timers.size;
  }

  /** Moves time on by `ms`, running each timer that comes due. */
  advance(ms: number): void {
    const end = this.now + ms;
    for (;;) {
      let first: [number, { at: number; fn: () => void }] | null = null;
      for (const entry of this.timers) if (entry[1].at <= end && (!first || entry[1].at < first[1].at)) first = entry;
      if (!first) break;
      this.timers.delete(first[0]);
      this.now = first[1].at;
      first[1].fn();
    }
    this.now = end;
  }
}

/** Everything a module can write to in a browser page, as stand-ins with
 *  nothing in them: `scan` says where a value went. */
export function fakeBrowser(clock: FakeClock = new FakeClock()) {
  const stored = (): Storage => {
    const m = new Map<string, string>();
    return {
      get length() {
        return m.size;
      },
      key: (i: number) => [...m.keys()][i] ?? null,
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, String(v)),
      removeItem: (k: string) => void m.delete(k),
      clear: () => m.clear(),
    };
  };
  const addresses: string[] = [];
  const history = {
    state: null as unknown,
    replaceState(data: unknown, _unused: string, url: string) {
      this.state = data;
      addresses.push(url);
    },
    pushState(data: unknown, _unused: string, url: string) {
      this.state = data;
      addresses.push(url);
    },
  };
  const listeners: Record<string, ((e: unknown) => void)[]> = {};
  const appended: Record<string, unknown>[] = [];
  const document = {
    title: "",
    cookie: "",
    visibilityState: "visible",
    head: { appendChild: (el: Record<string, unknown>) => void appended.push(el) },
    createElement: (tag: string) => ({ tag, remove() {} }) as Record<string, unknown>,
  };
  const window: Record<string, unknown> = {
    name: "",
    history,
    document,
    localStorage: stored(),
    sessionStorage: stored(),
    setTimeout: clock.set,
    clearTimeout: clock.clear,
    addEventListener: (type: string, fn: (e: unknown) => void) => void (listeners[type] ??= []).push(fn),
    removeEventListener: (type: string, fn: (e: unknown) => void) =>
      void (listeners[type] = (listeners[type] ?? []).filter((f) => f !== fn)),
  };
  return {
    clock,
    window,
    document,
    history,
    /** Script elements put in the page's head. */
    appended,
    dispatch(type: string, e: unknown) {
      for (const fn of listeners[type] ?? []) fn(e);
    },
    /** Where `needle` can be found in what the page could keep: the window's
     *  own values, the title and the cookie, both storages, the history's
     *  state and every address put in it. */
    scan(needle: string): string[] {
      const found: string[] = [];
      const seen = (where: string, v: unknown) => {
        let text = "";
        try {
          text = typeof v === "string" ? v : JSON.stringify(v) ?? "";
        } catch {
          text = String(v);
        }
        if (text.includes(needle)) found.push(where);
      };
      for (const [k, v] of Object.entries(window)) if (typeof v !== "function" && typeof v !== "object") seen(`window.${k}`, v);
      seen("document.title", document.title);
      seen("document.cookie", document.cookie);
      for (const s of ["localStorage", "sessionStorage"] as const) {
        const st = window[s] as Storage;
        for (let i = 0; i < st.length; i++) seen(`${s}.${st.key(i)}`, st.getItem(st.key(i) ?? ""));
      }
      seen("history.state", history.state);
      addresses.forEach((a, i) => seen(`history address ${i}`, a));
      return found;
    },
  };
}

/** Runs `fn` with `window`, `document`, `history` and the storages of a
 *  `fakeBrowser` as node's globals, put back as they were after. */
export async function inBrowser<T>(b: ReturnType<typeof fakeBrowser>, fn: () => T | Promise<T>): Promise<T> {
  const g = globalThis as unknown as Record<string, unknown>;
  const names = ["window", "document", "history", "localStorage", "sessionStorage"];
  const before = names.map((n) => Object.getOwnPropertyDescriptor(g, n));
  const values: Record<string, unknown> = {
    window: b.window,
    document: b.document,
    history: b.history,
    localStorage: b.window.localStorage,
    sessionStorage: b.window.sessionStorage,
  };
  for (const n of names) Object.defineProperty(g, n, { value: values[n], configurable: true, writable: true });
  try {
    return await fn();
  } finally {
    names.forEach((n, i) => {
      const d = before[i];
      if (d) Object.defineProperty(g, n, d);
      else delete g[n];
    });
  }
}

/** A refusal as axios hands it to the page, made by axios's own class: the
 *  status, and the body the server's error_body writes (`code`, `message`),
 *  with `more.detail`'s fields beside them in `detail` (a `reason`, say) and
 *  `more.headers` as the answer's (a `Retry-After`, say; axios hands them
 *  with their names in lower case). */
export function refusal(
  status: number,
  code: string,
  message = code,
  more: { detail?: Record<string, unknown>; headers?: Record<string, string> } = {},
): unknown {
  const data = { code, message, detail: { code, message, ...more.detail } };
  const headers = more.headers ?? {};
  return new AxiosError(message, undefined, undefined, undefined, { status, statusText: "", headers, config: {}, data } as AxiosResponse);
}

/** Lets every promise already settled run its callbacks (node's own timer,
 *  never a fake one: it runs after all the microtasks queued). */
export function settle(): Promise<void> {
  return new Promise((done) => globalThis.setTimeout(done, 0));
}
