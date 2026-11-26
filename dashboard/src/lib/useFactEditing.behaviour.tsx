// Forgetting a fact asks first (useFactEditing.ts), on the Facts page and in
// the graph's side panel alike: run in node by scripts/check_the_behaviour.mjs
// (`npm test`). Up to 1.0.28, on a phone one tap on the
// empty end of a fact, where a Forget button hid that only a mouse could
// reveal, forgot it with no question.
import type { AxiosResponse, InternalAxiosRequestConfig } from "axios";
import { renderToString } from "react-dom/server";
import apiClient from "@/lib/apiClient";
import type { Fact } from "@/lib/agentsApi";
import { fakeBrowser, inBrowser, type Check } from "@/lib/behaviour";
import { forgetAsking, forgetQuestion, useFactEditing } from "@/lib/useFactEditing";

const FACT = {
  id: "f-01",
  subject: "Giulia Rossi",
  predicate: "prefers_contact_channel",
  object: "email",
} as Fact;

/** Forget FACT through the hook, the person answering `yes` to whatever
 *  is asked: what was asked, what the real client sent, and how many times
 *  the list was read again. */
async function forget(yes: boolean) {
  const asked: string[] = [];
  const sent: string[] = [];
  let reloaded = 0;
  let editing: ReturnType<typeof useFactEditing> | null = null;
  function Probe() {
    editing = useFactEditing(() => void (reloaded += 1));
    return null;
  }
  renderToString(<Probe />);
  const g = globalThis as unknown as Record<string, unknown>;
  const confirmBefore = Object.getOwnPropertyDescriptor(g, "confirm");
  const adapterBefore = apiClient.defaults.adapter;
  g.confirm = (q: string) => {
    asked.push(q);
    return yes;
  };
  apiClient.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    const url = new URL(apiClient.getUri(config), "http://screen.test");
    sent.push(`${String(config.method).toUpperCase()} ${url.pathname}`);
    const data = { id: FACT.id, status: "invalidated", audit_id: "a-1" };
    return { data, status: 200, statusText: "OK", headers: {}, config } as AxiosResponse;
  };
  try {
    await inBrowser(fakeBrowser(), () => editing!.forget(FACT));
  } finally {
    apiClient.defaults.adapter = adapterBefore;
    if (confirmBefore) Object.defineProperty(g, "confirm", confirmBefore);
    else delete g.confirm;
  }
  return { asked, sent, reloaded };
}

export const CHECKS: Record<string, Check> = {
  "forgetting a fact asks first, naming it, and a no sends nothing": async (t) => {
    const { asked, sent, reloaded } = await forget(false);
    t.deepEqual(asked, [forgetQuestion(FACT)], "asked once, before anything is sent");
    t.ok(asked[0].includes("Giulia Rossi prefers_contact_channel email"), asked[0]);
    t.deepEqual(sent, [], "nothing forgotten");
    t.equal(reloaded, 0);
  },
  "a yes forgets it once and reads the list again": async (t) => {
    const { asked, sent, reloaded } = await forget(true);
    t.equal(asked.length, 1);
    t.deepEqual(sent, ["POST /admin/facts/f-01/forget"]);
    t.equal(reloaded, 1);
  },
  "a no leaves the fact as it was on the screen too, and a yes marks it busy only while it is sent": async (t) => {
    const steps = (yes: boolean, fails = false) => {
      const done: string[] = [];
      const run = forgetAsking(FACT, {
        ask: () => (done.push("ask"), yes),
        busy: (id) => void done.push(`busy ${id}`),
        err: (e) => void done.push(`err ${e}`),
        send: async (id) => {
          done.push(`send ${id}`);
          if (fails) throw new Error("down");
        },
        reload: () => void done.push("reload"),
      });
      return run.then(() => done);
    };
    t.deepEqual(await steps(false), ["ask"], "no busy, no error cleared, nothing sent");
    t.deepEqual(await steps(true), ["ask", "busy f-01", "err null", "send f-01", "reload", "busy null"]);
    t.deepEqual(await steps(true, true), [
      "ask", "busy f-01", "err null", "send f-01", "err Could not forget the fact.", "busy null",
    ]);
  },
};
