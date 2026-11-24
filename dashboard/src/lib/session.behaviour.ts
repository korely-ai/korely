// What the screen does with the session it kept when the server is asked
// about it (session.ts): run in node by scripts/check_the_behaviour.mjs
// (`npm test`). QA of 14 Nov 2026: a 500, a 429, a 503 or a dropped network
// on that read cleared the token and showed the sign-in page with nothing
// said. Only a 401 ends the session.
import { AxiosError, type AxiosResponse } from "axios";
import apiClient, { TOKEN_KEY } from "@/lib/apiClient";
import { NOT_REACHED } from "@/lib/apiError";
import { fakeBrowser, inBrowser, refusal, settle, type Check } from "@/lib/behaviour";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { RequireAuth } from "@/components/RequireAuth";
import { AuthContext, type AuthContextValue } from "@/contexts/AuthContext";
import {
  ACCOUNT_GONE,
  ONLY_THE_401,
  OR_THE_ACCOUNT_GONE,
  SESSION_UNCHECKED,
  askAboutTheSession,
  gateOf,
  readSession,
  type SessionKeeper,
  type SessionShown,
} from "@/lib/session";

const NOW = Date.UTC(2026, 10, 14, 0, 30, 10);
const now = () => NOW;
const failing = (e: unknown) => () => Promise.reject(e);
const dropped = () => new AxiosError("Network Error", "ERR_NETWORK");

/** The session read through the real client, its network answering
 *  `status` (null: no answer), in a browser that keeps a token: what the
 *  read said, and the token left after it. */
async function throughTheClient(status: number | null) {
  const b = fakeBrowser();
  b.window.dispatchEvent = (e: Event) => b.dispatch(e.type, e);
  (b.window.localStorage as Storage).setItem(TOKEN_KEY, "kor_sess_kept");
  const before = apiClient.defaults.adapter;
  apiClient.defaults.adapter = async (config) => {
    if (status === null) throw new AxiosError("Network Error", "ERR_NETWORK", config);
    const body = status === 200 ? { name: "op", tenant_id: "t", kind: "password" } : { code: "x", message: "refused" };
    const r = { status, statusText: "", headers: {}, config, data: body } as AxiosResponse;
    if (status === 200) return r;
    throw new AxiosError("refused", undefined, config, undefined, r);
  };
  try {
    return await inBrowser(b, async () => {
      const said = await readSession(() => apiClient.get("/admin/whoami").then((r) => r.data), now);
      return { said, token: localStorage.getItem(TOKEN_KEY) };
    });
  } finally {
    apiClient.defaults.adapter = before;
  }
}

const last = <T,>(xs: T[]): T | undefined => xs[xs.length - 1];

/** A keeper and a screen that write down what they were told. */
function watched() {
  const seen: string[] = [];
  const shown: SessionShown[] = [];
  const keeper: SessionKeeper<unknown> = {
    signIn: () => void seen.push("in"),
    signOut: (said) => void seen.push(`out:${said}`),
  };
  return { seen, shown, keeper, show: (s: SessionShown) => void shown.push(s) };
}

export const CHECKS: Record<string, Check> = {
  "a 401 ends the session": async (t) => {
    t.deepEqual(await readSession(failing(refusal(401, "unauthorized", "Invalid session")), now), { signedOut: null });
  },
  "a 500, a 503 and a dropped network keep it, and say so": async (t) => {
    for (const [e, says] of [
      [refusal(500, "internal_error", "internal error"), "The install could not answer (HTTP 500)."],
      [refusal(503, "unavailable", "Down for maintenance"), "Down for maintenance."],
      [dropped(), NOT_REACHED],
    ] as const) {
      const a = await readSession(failing(e), now);
      t.ok("unsure" in a, `${says}: not signed out`);
      if (!("unsure" in a)) continue;
      t.equal(a.unsure.text, `${SESSION_UNCHECKED} ${says} You are still signed in on this browser.`);
      t.equal(a.unsure.again, "now");
    }
  },
  "a 400, a 403 and a 404 of the install keep it too: only its 401 ends it": async (t) => {
    for (const status of [400, 403, 404, 409, 422]) {
      const a = await readSession(failing(refusal(status, "not_found", "No.")), now);
      t.ok("unsure" in a, `${status} kept the session`);
    }
  },
  "on the Cloud the account gone ends it, and nothing else of the kind": async (t) => {
    const gone = await readSession(failing(refusal(404, "not_found", "No such account.")), now, OR_THE_ACCOUNT_GONE);
    t.deepEqual(gone, { signedOut: ACCOUNT_GONE });
    for (const [status, code] of [[404, "other"], [403, "not_found"], [500, "not_found"]] as const) {
      const a = await readSession(failing(refusal(status, code, "x")), now, OR_THE_ACCOUNT_GONE);
      t.ok("unsure" in a, `${status} ${code} kept the session`);
    }
  },
  "an error of this browser is said as one, never 'HTTP undefined'": async (t) => {
    const a = await readSession(failing(new TypeError("boom")), now);
    t.ok("unsure" in a);
    if (!("unsure" in a)) return;
    t.ok(!a.unsure.text.includes("undefined"), a.unsure.text);
    t.match(a.unsure.text, /in this browser/);
  },
  "a 429 keeps it, and says when to ask again": async (t) => {
    const e = refusal(429, "rate_limited", "Too many requests", { headers: { "retry-after": "37" } });
    const a = await readSession(failing(e), now);
    t.ok("unsure" in a);
    if (!("unsure" in a)) return;
    t.equal(a.unsure.again, Math.ceil((NOW + 37_000) / 60_000) * 60_000);
    t.match(a.unsure.text, /Too many requests\. Try again after .* \(in a minute\)\. You are still signed in/);
  },
  "an answer signs in with what it said": async (t) => {
    t.deepEqual(await readSession(() => Promise.resolve({ name: "op" }), now), { signedIn: { name: "op" } });
  },
  "the provider is told only in or out, and the screen why it could not tell": async (t) => {
    const w = watched();
    askAboutTheSession(failing(refusal(500, "internal_error")), w.keeper, w.show, () => true, ONLY_THE_401, now);
    await settle();
    t.deepEqual(w.seen, [], "a 500 neither signs in nor out");
    t.deepEqual(w.shown[0], { loading: true, failure: null });
    t.equal(last(w.shown)?.loading, false);
    t.match(last(w.shown)?.failure?.text ?? "", /^Your session could not be checked\./);

    const out = watched();
    askAboutTheSession(failing(refusal(401, "unauthorized")), out.keeper, out.show, () => true, ONLY_THE_401, now);
    await settle();
    t.deepEqual(out.seen, ["out:null"]);
    t.deepEqual(last(out.shown), { loading: false, failure: null });

    const gone = watched();
    askAboutTheSession(failing(refusal(404, "not_found")), gone.keeper, gone.show, () => true, OR_THE_ACCOUNT_GONE, now);
    await settle();
    t.deepEqual(gone.seen, [`out:${ACCOUNT_GONE}`], "a dead account is no failure to try again");
    t.equal(last(gone.shown)?.failure, null);

    const inn = watched();
    askAboutTheSession(() => Promise.resolve({}), inn.keeper, inn.show, () => true, ONLY_THE_401, now);
    await settle();
    t.deepEqual(inn.seen, ["in"]);
    t.deepEqual(last(inn.shown), { loading: false, failure: null });
  },
  "no session kept, nothing asked; a provider that left takes no answer": async (t) => {
    const w = watched();
    let asked = 0;
    askAboutTheSession(() => Promise.resolve(++asked), w.keeper, w.show, () => false);
    await settle();
    t.equal(asked, 0);
    t.deepEqual(w.shown, [{ loading: false, failure: null }]);
    const left = watched();
    const cancel = askAboutTheSession(() => Promise.resolve({}), left.keeper, left.show, () => true);
    cancel();
    await settle();
    t.deepEqual(left.seen, []);
    t.equal(left.shown.length, 1, "only the first 'asking'");
  },
  "the gate: wait, the reason kept, the page, or the sign-in": (t) => {
    const f = { text: "x", again: "now" as const };
    t.equal(gateOf({ user: null, loading: true, sessionFailure: f }), "wait");
    t.equal(gateOf({ user: null, loading: false, sessionFailure: f }), "unsure");
    t.equal(gateOf({ user: { name: "op" }, loading: false, sessionFailure: f }), "in");
    t.equal(gateOf({ user: null, loading: false, sessionFailure: null }), "out");
  },
  "the page behind the sign-in shows the reason and keeps the session": async (t) => {
    // React warns that a router's layout effects do nothing on a server:
    // true, and nothing here needs them.
    const quiet = <T,>(fn: () => T): T => {
      const warn = console.error;
      console.error = () => {};
      try {
        return fn();
      } finally {
        console.error = warn;
      }
    };
    const drawn = (value: Partial<AuthContextValue>) =>
      quiet(() => renderToString(
        createElement(
          MemoryRouter,
          null,
          createElement(AuthContext.Provider, { value: { ...SIGNED_OUT, ...value } as AuthContextValue }, createElement(RequireAuth)),
        ),
      )).replace(/<!-- -->/g, "");
    const unsure = drawn({ sessionFailure: { text: "Your session could not be checked. HTTP 500.", again: "now" } });
    t.ok(unsure.includes("Your session could not be checked. HTTP 500."), unsure);
    t.ok(unsure.includes("Try again") && unsure.includes("Sign out of this browser instead"));
    t.ok(!drawn({}).includes("could not be checked"), "signed out: the sign-in page, nothing said here");
    t.ok(drawn({ loading: true }).includes("Loading"));
  },
  "through the client, only a 401 takes the token away": async (t) => {
    for (const status of [500, 429, 503, 403, 404, null]) {
      const r = await throughTheClient(status);
      t.ok("unsure" in r.said, `${status}: unsure`);
      t.equal(r.token, "kor_sess_kept", `${status}: the token is kept`);
    }
    const out = await throughTheClient(401);
    t.deepEqual(out.said, { signedOut: null });
    t.equal(out.token, null, "a 401 removes it");
    const ok = await throughTheClient(200);
    t.ok("signedIn" in ok.said);
    t.equal(ok.token, "kor_sess_kept");
  },
};

const SIGNED_OUT: AuthContextValue = {
  user: null,
  loading: false,
  authError: null,
  login: async () => {},
  logout: async () => {},
  logoutEverywhere: async () => {},
  sessionFailure: null,
  checkSessionAgain: () => {},
};
