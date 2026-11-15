// How the screen says a failed call (apiError.ts `failure`, and the client's
// refusal of an answer that is not data, apiClient.ts): run in node by
// scripts/check_the_behaviour.mjs (`npm test`). QA of 14 Nov 2026: a 429's
// Retry-After was never read, a 403 was offered "Try again" forever, and a
// 200 with a web page emptied the whole screen.
import { AxiosError, type AxiosResponse } from "axios";
import apiClient, { notData } from "@/lib/apiClient";
import { LONGEST_WAIT_S, NOT_DATA, NOT_DATA_SAID, afterWords, againAt, apiRetryAfter, asSentence, failure, wasNotData } from "@/lib/apiError";
import { fakeBrowser, inBrowser, refusal, type Check } from "@/lib/behaviour";

const NOW = Date.UTC(2026, 10, 14, 0, 30, 10);
const MINUTE = 60_000;

const answer = (data: unknown, type: string | null, status = 200) =>
  ({ status, statusText: "", headers: type ? { "content-type": type } : {}, config: {}, data }) as AxiosResponse;

/** apiClient with the network replaced by `answer`, in a stand-in browser. */
async function through(a: AxiosResponse): Promise<unknown> {
  const before = apiClient.defaults.adapter;
  apiClient.defaults.adapter = async (config) => {
    const r = { ...a, config };
    if (r.status >= 200 && r.status < 300) return r;
    throw new AxiosError("refused", undefined, config, undefined, r);
  };
  try {
    return await inBrowser(fakeBrowser(), () =>
      apiClient.get("/admin/memories").then(
        (r) => ({ ok: r.data }),
        (e) => ({ failed: e }),
      ),
    );
  } finally {
    apiClient.defaults.adapter = before;
  }
}

export const CHECKS: Record<string, Check> = {
  "a 429 that names a wait says until when, and is not offered before it": (t) => {
    const e = refusal(429, "rate_limited", "Too many requests", { headers: { "retry-after": "37" } });
    const f = failure(e, "Could not load memories.", NOW);
    const at = Math.ceil((NOW + 37_000) / MINUTE) * MINUTE;
    t.equal(f.again, at, "the wait, rounded up to the minute said");
    t.equal(f.text, `Too many requests. Try again ${afterWords(at, NOW)}.`);
    t.equal(againAt(e, NOW), at);
  },
  "a 503 that names a wait as a date is read the same way": (t) => {
    // An HTTP date has whole seconds: 00:35:10 here, said as the minute after.
    const when = new Date(NOW + 5 * MINUTE).toUTCString();
    const e = refusal(503, "unavailable", "Down for a moment", { headers: { "retry-after": when } });
    const at = Math.ceil((NOW + 5 * MINUTE) / MINUTE) * MINUTE;
    const f = failure(e, "x", NOW);
    t.equal(f.again, at);
    t.equal(f.text, `Down for a moment. Try again ${afterWords(at, NOW)}.`);
  },
  "a wait past any the server names is read as a year, never an invalid date": (t) => {
    for (const raw of ["99999999999999", new Date(NOW + 5 * 366 * 86_400_000).toUTCString()]) {
      const e = refusal(429, "rate_limited", "Too many requests", { headers: { "retry-after": raw } });
      t.equal(apiRetryAfter(e, NOW), LONGEST_WAIT_S, raw);
      const f = failure(e, "x", NOW);
      t.ok(typeof f.again === "number" && Number.isFinite(f.again), raw);
      t.ok(!f.text.includes("Invalid"), f.text);
    }
    const month = refusal(429, "rate_limited", "x", { headers: { "retry-after": String(31 * 86_400) } });
    t.equal(apiRetryAfter(month, NOW), 31 * 86_400, "a month is read as it is");
  },
  "a 429 that names no wait may be tried again at once": (t) => {
    const f = failure(refusal(429, "rate_limited", "Too many requests"), "x", NOW);
    t.deepEqual(f, { text: "Too many requests", again: "now" });
  },
  "a 403 is said in the server's words and never offered again": (t) => {
    const f = failure(refusal(403, "install_admin_only", "Only the install's administrator sets them."), "x", NOW);
    t.deepEqual(f, { text: "Only the install's administrator sets them.", again: "never" });
  },
  "a 500 with no reason says the page's own sentence": (t) => {
    const f = failure(refusal(500, "internal_error", "internal error"), "Could not load memories.", NOW);
    t.deepEqual(f, { text: "Could not load memories.", again: "now" });
  },
  "a sentence of the server's is closed before the page's next one": (t) => {
    t.equal(asSentence("Too many requests"), "Too many requests.");
    t.equal(asSentence("Done."), "Done.");
    t.equal(asSentence(" Why? "), "Why?");
  },
  "an answer that is a web page is not data, and nothing else is": (t) => {
    t.equal(notData(answer("<html>proxy page</html>", "text/html")), true);
    t.equal(notData(answer("<html>proxy page</html>", null)), true, "no type");
    t.equal(notData(answer({ memories: [] }, "application/json")), false);
    t.equal(notData(answer("", "text/html")), false, "an empty body (a 204) is no page");
    t.equal(notData(answer("\"a string\"", "application/json")), false, "a JSON type is data");
  },
  "through the client, a 200 with a web page is a refusal the page says": async (t) => {
    const r = (await through(answer("<html>proxy page</html>", "text/html"))) as { failed?: unknown };
    t.ok(r.failed, "the read fails instead of handing the page a string");
    t.ok(wasNotData(r.failed));
    t.equal((r.failed as AxiosError).code, NOT_DATA);
    t.deepEqual(failure(r.failed, "Could not load memories.", NOW), { text: NOT_DATA_SAID, again: "now" });
  },
  "through the client, JSON still arrives as data": async (t) => {
    const r = (await through(answer({ memories: [] }, "application/json; charset=utf-8"))) as { ok?: unknown };
    t.deepEqual(r.ok, { memories: [] });
  },
};
