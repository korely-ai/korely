// What the API Keys page and Home say of a key's end, and what Rotate sends
// and shows (lib/keyLife.ts, components/KeyEnds.tsx): run in node by
// scripts/check_the_behaviour.mjs (`npm test`). Until 1.0.26 the screen had
// neither: a date in a column, and no Rotate (the decision of 2 Nov 2026).
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import apiClient from "@/lib/apiClient";
import { keysApi, type AgentKey, type AgentKeyCreated, type KeyLifetimes } from "@/lib/agentsApi";
import { fakeBrowser, inBrowser, refusal, type Check } from "@/lib/behaviour";
import { EndBadge, KeysEndingNote, OldSecretChoice, OldSecretLine } from "@/components/KeyEnds";
import {
  NO_ANSWER_NOT_ROTATED,
  NO_ANSWER_ROTATED_NOW,
  NO_ANSWER_UNKNOWN,
  NOW_CHOICE,
  afterNoAnswer,
  afterRotation,
  endNear,
  endingSoon,
  inWords,
  oldSecretUntil,
  overlapChoices,
  overlapInForce,
  rotateFromTheScreen,
} from "@/lib/keyLife";

const NOW = Date.UTC(2026, 10, 15, 2, 0, 0);
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const NOTICE = 14;
const html = (el: ReactElement) => renderToString(el).replace(/<!-- -->/g, "");
const at = (ms: number) => new Date(NOW + ms).toISOString();

function key(name: string, over: Partial<AgentKey> = {}): AgentKey {
  return {
    id: `id-${name}`, name, masked: `••••${name}`, tier: "hobby", scopes: [],
    created_at: at(-DAY), last_used_at: null, revoked: false, project_id: null,
    project_name: null, state: "live", rotates: true, expires_at: null, ...over,
  };
}

const LIFETIMES: KeyLifetimes = {
  default_days: 90, choices: [], warning_days: NOTICE, max_days: 3650,
  old_secret_default_hours: 24,
  old_secret_choices: [
    { hours: 0, label: "Now" }, { hours: 1, label: "1 hour" }, { hours: 24, label: "24 hours" },
    { hours: 72, label: "3 days" }, { hours: 168, label: "7 days" },
  ],
};

/** What the real client sends for `run`, its network answering with a
 *  rotated key: each request's method, path and body. */
async function sent(run: () => Promise<unknown>) {
  const seen: { method: string; path: string; body: unknown }[] = [];
  const before = apiClient.defaults.adapter;
  apiClient.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    const url = new URL(apiClient.getUri(config), "http://screen.test");
    seen.push({
      method: String(config.method).toUpperCase(),
      path: url.pathname,
      body: config.data === undefined ? undefined : JSON.parse(String(config.data)),
    });
    const data = { ...key("rotated"), key: "secret-new" } as AgentKeyCreated;
    return { data, status: 200, statusText: "OK", headers: {}, config } as AxiosResponse;
  };
  try {
    await inBrowser(fakeBrowser(), run);
  } finally {
    apiClient.defaults.adapter = before;
  }
  return seen;
}

export const CHECKS: Record<string, Check> = {
  "Rotate sends the rotate call with the overlap chosen, 0 for a leaked key": async (t) => {
    t.deepEqual(await sent(() => keysApi.rotate("k1", 0)), [
      { method: "POST", path: "/admin/keys/k1/rotate", body: { old_key_expires_in_hours: 0 } },
    ]);
    t.deepEqual(await sent(() => keysApi.rotate("k1", 72)), [
      { method: "POST", path: "/admin/keys/k1/rotate", body: { old_key_expires_in_hours: 72 } },
    ]);
    t.deepEqual(await sent(() => keysApi.rotate("k1")), [
      { method: "POST", path: "/admin/keys/k1/rotate", body: {} },
    ], "no choice read: the install's default overlap, the field not sent");
    t.deepEqual((await sent(() => keysApi.rotate("a/b", 0)))[0].path, "/admin/keys/a%2Fb/rotate",
      "the id is one path segment");
  },
  "the new secret is handed on once, and a refusal hands on none": async (t) => {
    const shown: AgentKeyCreated[] = [];
    const failed: [string, boolean][] = [];
    const said = { shown: (k: AgentKeyCreated) => void shown.push(k), failed: (x: string, again: boolean) => void failed.push([x, again]) };
    const made = { ...key("a"), key: "secret-a" } as AgentKeyCreated;
    const asked: [string, number | undefined][] = [];
    const listed: string[] = [];
    const list = async () => (listed.push("read"), [key("a")]);
    await rotateFromTheScreen({ rotate: async (id, hours) => (asked.push([id, hours]), made), list },
      key("a"), 0, { current: false }, said, () => NOW);
    t.deepEqual(asked, [["id-a", 0]], "the overlap chosen reaches the call");
    t.deepEqual(shown.map((k) => k.key), ["secret-a"]);
    t.deepEqual(failed, []);

    const refused = (e: unknown) => ({ rotate: () => Promise.reject(e), list });
    await rotateFromTheScreen(refused(refusal(409, "key_not_rotatable", "only a live key rotates; this one is expired")),
      key("a"), 24, { current: false }, said, () => NOW);
    t.deepEqual(failed[0], ["only a live key rotates; this one is expired", true], "the server's own words");
    await rotateFromTheScreen(
      refused(refusal(429, "rate_limited", "Too many requests", { headers: { "retry-after": "90" } })),
      key("a"), 24, { current: false }, said, () => NOW);
    t.match(failed[1][0], /^Too many requests\. Try again after .+ \(in 2 minutes\)\.$/, "the wait it named");
    t.deepEqual(listed, [], "a refusal of the install's own is known: the key is not read again for it");
    t.equal(shown.length, 1, "nothing shown for a rotation that did not happen");
  },
  "a rotation with no answer is not called a failure: the key is read again first": async (t) => {
    // What a lost answer looks like: no answer at all, a proxy's 502 with
    // a web page, and the unhandled 500, which can come after the write.
    const lost = [
      new AxiosError("Network Error", "ERR_NETWORK"),
      new AxiosError("timeout of 30000ms exceeded", "ECONNABORTED"),
      new AxiosError("Bad Gateway", undefined, undefined, undefined,
        { status: 502, statusText: "", headers: {}, config: {}, data: "<html>502</html>" } as AxiosResponse),
      refusal(500, "internal_error", "internal error"),
    ];
    const before = key("a", { masked: "••••AAAA", old_secret_expires_at: null });
    for (const e of lost) {
      const failed: [string, boolean][] = [];
      const held = { current: false };
      const said = { shown: () => {}, failed: (x: string, again: boolean) => void failed.push([x, again]) };
      // The answer lost, the rotation done with an overlap: the agents'
      // secret works a day more, and a rotation again would end it at once.
      const rotated = key("a", { masked: "••••BBBB", old_secret_expires_at: at(DAY) });
      await rotateFromTheScreen({ rotate: () => Promise.reject(e), list: async () => [rotated] },
        before, 24, held, said, () => NOW);
      t.equal(failed.length, 1, String(e));
      t.match(failed[0][0], /^No answer came back, but the key was rotated: .+ works until .+, and rotating again would end it at once: .+ new key before then\.$/);
      t.equal(failed[0][1], false, "no Rotate again from the dialog");
      t.equal(held.current, true, "held: a click more sends nothing");
      // Not rotated after all: the same secret listed, the next try may go.
      const again = { current: false };
      await rotateFromTheScreen({ rotate: () => Promise.reject(e), list: async () => [before] },
        before, 24, again, said, () => NOW);
      t.deepEqual(failed[1], [NO_ANSWER_NOT_ROTATED, true]);
      t.equal(again.current, false);
      // Not read again either: it may have been, so no Rotate again.
      const unread = { current: false };
      await rotateFromTheScreen({ rotate: () => Promise.reject(e), list: () => Promise.reject(e) },
        before, 24, unread, said, () => NOW);
      t.deepEqual(failed[2], [NO_ANSWER_UNKNOWN, false]);
      t.equal(unread.current, true);
    }
  },
  "what the key read again says of a rotation that got no answer": (t) => {
    const before = key("a", { masked: "••••AAAA", old_secret_expires_at: null });
    t.deepEqual(afterNoAnswer(before, [key("b"), before], NOW), { text: NO_ANSWER_NOT_ROTATED, again: true });
    t.deepEqual(afterNoAnswer(before, [key("a", { masked: "••••BBBB" })], NOW),
      { text: NO_ANSWER_ROTATED_NOW, again: true }, "rotated with Now: nothing more to lose, rotate again");
    t.equal(afterNoAnswer(before, [key("a", { masked: "••••AAAA", old_secret_expires_at: at(HOUR) })], NOW).again, false,
      "the last four alike by chance: the overlap that started says it");
    t.deepEqual(afterNoAnswer(before, [key("b")], NOW), { text: NO_ANSWER_UNKNOWN, again: false }, "the key not listed");
    t.deepEqual(afterNoAnswer(before, null, NOW), { text: NO_ANSWER_UNKNOWN, again: false }, "not read");
    // An earlier rotation's overlap still running, then rotated again: its
    // end moves to the new overlap's.
    const during = key("a", { masked: "••••AAAA", old_secret_expires_at: at(HOUR) });
    t.equal(afterNoAnswer(during, [during], NOW).again, true, "not rotated: the same overlap");
    t.equal(afterNoAnswer(during, [key("a", { masked: "••••CCCC", old_secret_expires_at: at(DAY) })], NOW).again,
      false);
    // The earlier overlap ended while the call was out: not a rotation.
    t.equal(afterNoAnswer(during, [key("a", { masked: "••••AAAA", old_secret_expires_at: null })], NOW).text,
      NO_ANSWER_NOT_ROTATED);
  },
  "a key ending in 3 days is marked, one ending in 30 is not": (t) => {
    const soon = endNear(key("a", { expires_at: at(3 * DAY) }), NOTICE, NOW);
    t.deepEqual(soon, { at: NOW + 3 * DAY, words: "in 3 days", lastDay: false });
    t.equal(endNear(key("b", { expires_at: at(30 * DAY) }), NOTICE, NOW), null);
    t.ok(endNear(key("c", { expires_at: at(NOTICE * DAY) }), NOTICE, NOW), "on the notice's first moment");
    t.equal(endNear(key("d", { expires_at: at(NOTICE * DAY + 1) }), NOTICE, NOW), null, "a moment before it");
    t.equal(endNear(key("e", { expires_at: null }), NOTICE, NOW), null, "a key that never ends");
    t.equal(endNear(key("f", { expires_at: at(-1) }), NOTICE, NOW), null, "past: the page says Expired");
    t.equal(endNear(key("g", { expires_at: at(40 * 60_000), rotates: false }), NOTICE, NOW), null,
      "a connected app's key renews itself every hour: no warning");
    t.equal(endNear(key("h", { expires_at: at(3 * DAY) }), null, NOW), null, "the notice unread");
    t.equal(endNear(key("i", { expires_at: at(3 * DAY) }), 2, NOW), null, "the notice is the server's");
  },
  "the last day is said in hours, and in red": (t) => {
    t.deepEqual(endNear(key("a", { expires_at: at(12 * HOUR) }), NOTICE, NOW),
      { at: NOW + 12 * HOUR, words: "in 12 hours", lastDay: true });
    t.equal(endNear(key("b", { expires_at: at(DAY) }), NOTICE, NOW)?.lastDay, false, "a whole day left");
    t.equal(endNear(key("c", { expires_at: at(DAY - 1) }), NOTICE, NOW)?.lastDay, true);
    t.equal(inWords(DAY + 23 * HOUR), "in 1 day", "rounded down, never more than is left");
    t.equal(inWords(HOUR), "in 1 hour");
    t.equal(inWords(59 * 60_000), "in under an hour");
    const red = html(<EndBadge near={endNear(key("d", { expires_at: at(5 * HOUR) }), NOTICE, NOW)} />);
    t.ok(red.includes("Ends in 5 hours") && red.includes("text-destructive"), red);
    const amber = html(<EndBadge near={endNear(key("e", { expires_at: at(3 * DAY) }), NOTICE, NOW)} />);
    t.ok(amber.includes("Ends in 3 days") && !amber.includes("text-destructive"), amber);
    t.equal(html(<EndBadge near={null} />), "");
  },
  "Home names the keys ending soon, the nearest first, and where to rotate them": (t) => {
    const keys = [
      key("far", { expires_at: at(30 * DAY) }),
      key("week", { expires_at: at(3 * DAY) }),
      key("app", { expires_at: at(HOUR), rotates: false }),
      key("tonight", { expires_at: at(12 * HOUR) }),
      key("gone", { expires_at: at(2 * DAY), revoked: true, state: "revoked", rotates: false }),
    ];
    const ending = endingSoon(keys, NOTICE, NOW);
    t.deepEqual(ending.map((e) => e.key.name), ["tonight", "week"]);
    const out = html(<MemoryRouter><KeysEndingNote ending={ending} /></MemoryRouter>);
    t.ok(out.includes("2 API keys stop working soon: tonight in 12 hours, week in 3 days."), out);
    t.ok(out.includes('href="/keys"') && out.includes("Rotate them on API Keys"), out);
    t.ok(out.includes("text-destructive"), "red with a key on its last day");
    const one = html(<MemoryRouter><KeysEndingNote ending={endingSoon([keys[1]], NOTICE, NOW)} /></MemoryRouter>);
    t.ok(one.includes("An API key stops working soon: week in 3 days.") && one.includes("Rotate it on"), one);
    t.ok(!one.includes("text-destructive"), one);
    t.equal(html(<MemoryRouter><KeysEndingNote ending={endingSoon([keys[0]], NOTICE, NOW)} /></MemoryRouter>), "",
      "nothing when no key ends soon");
    const many = ["a", "b", "c", "d", "e"].map((n, i) => key(n, { expires_at: at((i + 1) * DAY) }));
    const five = html(<MemoryRouter><KeysEndingNote ending={endingSoon(many, NOTICE, NOW)} /></MemoryRouter>);
    t.ok(five.includes("5 API keys stop working soon: a in 1 day, b in 2 days, c in 3 days and 2 more."), five);
  },
  "Rotate offers the server's choices, and Now even when they are unread": (t) => {
    const served = overlapChoices(LIFETIMES);
    t.deepEqual(served.choices.map((c) => c.hours), [0, 1, 24, 72, 168]);
    t.equal(served.start, 24, "the server's default, chosen first");
    const unread = overlapChoices(null);
    t.deepEqual(unread.choices[0], NOW_CHOICE, "a leaked key can still end its old secret now");
    t.deepEqual(unread.choices.map((c) => c.hours), [0, undefined]);
    t.equal(unread.start, undefined, "unread: the install's default, the field not sent");
    t.equal(overlapChoices({ ...LIFETIMES, old_secret_default_hours: 5 }).start, undefined,
      "a default that is no choice is not chosen");
  },
  "the choice sent is the one the menu shows, when the choices arrive late": (t) => {
    const unread = overlapChoices(null);
    const served = overlapChoices(LIFETIMES);
    t.equal(overlapInForce(unread, null), undefined, "nothing picked, unread: the install's default");
    t.equal(overlapInForce(served, null), 24, "nothing picked: the served default");
    t.equal(overlapInForce(unread, 0), 0);
    t.equal(overlapInForce(served, 0), 0, "Now picked before the choices came stays Now");
    t.equal(overlapInForce(served, undefined), 24, "the install's default is the served one");
    t.equal(overlapInForce(served, 72), 72);
    t.equal(overlapInForce(served, 5), 24, "a pick no longer offered is not sent");
  },
  "the menu shows the choice the dialog sends": (t) => {
    const served = overlapChoices(LIFETIMES);
    const shown = (offer: typeof served, picked: number | undefined | null) =>
      html(<OldSecretChoice offer={offer} picked={picked} onPick={() => {}} />);
    const selected = (out: string) => [...out.matchAll(/<option[^>]*value="([^"]+)"[^>]*selected=""/g)].map((m) => m[1]);
    t.deepEqual(selected(shown(served, null)), ["24"], "the served default, as overlapInForce sends");
    t.deepEqual(selected(shown(served, 72)), ["72"], "the pick, not the default");
    t.deepEqual(selected(shown(served, 5)), ["24"], "a pick no longer offered is neither shown nor sent");
    const now = shown(served, 0);
    t.deepEqual(selected(now), ["0"]);
    t.ok(now.includes("At once: the agents that hold it stop") && now.includes(">Now</option>"), now);
    t.ok(shown(served, 72).includes(">In 3 days</option>") && shown(served, 72).includes("both secrets work"));
    t.deepEqual(selected(shown(overlapChoices(null), null)), ["default"], "unread: the install's default");
  },
  "a rotated key says until when its old secret works": (t) => {
    t.equal(oldSecretUntil({ old_secret_expires_at: at(HOUR) }, NOW), NOW + HOUR);
    t.equal(oldSecretUntil({ old_secret_expires_at: at(-1) }, NOW), null, "ended");
    t.equal(oldSecretUntil({ old_secret_expires_at: null }, NOW), null);
    t.ok(html(<OldSecretLine until={NOW + HOUR} />).includes("Old secret works until"));
    t.equal(html(<OldSecretLine until={null} />), "");
    t.match(afterRotation({ old_secret_expires_at: at(HOUR) }, NOW), /^The old secret works until .+, then only this one does\.$/);
    t.equal(afterRotation({ old_secret_expires_at: null }, NOW),
      "The old secret has stopped working: only this one works now.", "rotated with Now");
  },
};
