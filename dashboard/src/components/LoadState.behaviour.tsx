// What a page shows for a failure (LoadState.tsx), what is left of a page
// that throws (PageBoundary.tsx), and the other sentences the QA of 14 Nov
// 2026 found missing: run in node by scripts/check_the_behaviour.mjs (`npm
// test`), drawn with renderToString.
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { FailureNote, LoadNote, LoadNotes, offerAgain, offerWhenDue, type Failed } from "@/components/LoadState";
import { PageBoundary, PageBroke, messageOf } from "@/components/PageBoundary";
import { loginFailure } from "@/contexts/AuthContext";
import { LONGEST_TIMER_MS, afterWords, atMoment } from "@/lib/apiError";
import { FakeClock, refusal, type Check } from "@/lib/behaviour";
import { NO_WEBGL2, drawsWebGL2 } from "@/lib/graphLook";
import { whatAddWaitsFor } from "@/pages/WebhooksPage";

const NOW = Date.now();
const TRY = "Try again";
const read = (over: Partial<Failed>): Failed => ({ error: null, reload: () => {}, refused: false, waitUntil: null, ...over });
// Without React's markers between text nodes: what a person reads.
const html = (el: ReactElement) => renderToString(el).replace(/<!-- -->/g, "");
const count = (text: string, part: string) => text.split(part).length - 1;

export const CHECKS: Record<string, Check> = {
  "a failure is offered again at will, after its wait, and never when refused": (t) => {
    t.equal(offerAgain("now", NOW), true);
    t.equal(offerAgain("never", NOW), false);
    t.equal(offerAgain(NOW + 1000, NOW), false, "during the wait");
    t.equal(offerAgain(NOW, NOW), true, "at its end");
  },
  "Try again comes back when the wait is over, and not a moment before": (t) => {
    const clock = new FakeClock();
    clock.now = NOW;
    const c = { now: () => clock.now, set: clock.set, clear: clock.clear };
    const told: boolean[] = [];
    offerWhenDue(NOW + 37_000, (o) => void told.push(o), c);
    t.deepEqual(told, [false], "not during the wait");
    clock.advance(36_999);
    t.deepEqual(told, [false]);
    clock.advance(1);
    t.deepEqual(told, [false, true], "at its end");
    const now: boolean[] = [];
    offerWhenDue("now", (o) => void now.push(o), c);
    offerWhenDue("never", (o) => void now.push(o), c);
    t.deepEqual(now, [true, false]);
    t.equal(clock.pending, 0, "nothing left armed");
  },
  "a wait longer than a timer keeps fires at its end, not at once": (t) => {
    const clock = new FakeClock();
    clock.now = NOW;
    // As a browser does: a delay past a signed 32-bit integer wraps, and
    // the timer fires at once.
    // A wait that re-arms itself at once would spin the fake clock forever
    // (the check could never end): past a thousand timers, it fails.
    let armed = 0;
    const c = {
      now: () => clock.now,
      set: (fn: () => void, ms: number) => {
        if (++armed > 1000) throw new Error("the wait re-arms itself at once, forever");
        return clock.set(fn, ms > LONGEST_TIMER_MS ? 0 : ms);
      },
      clear: clock.clear,
    };
    const fortyDays = 40 * 24 * 3_600_000;
    let fired = 0;
    const cancel = atMoment(NOW + fortyDays, () => void fired++, c);
    clock.advance(LONGEST_TIMER_MS);
    t.equal(fired, 0, "past one timer's longest delay, still waiting");
    clock.advance(fortyDays - LONGEST_TIMER_MS - 1);
    t.equal(fired, 0);
    clock.advance(1);
    t.equal(fired, 1);
    cancel();
    let never = 0;
    const stop = atMoment(NOW + fortyDays * 2, () => void never++, c);
    stop();
    clock.advance(fortyDays * 3);
    t.equal(never, 0, "a cancelled wait never fires");
  },
  "a failure that can be tried again offers it": (t) => {
    const out = html(<FailureNote failure={{ text: "Could not load.", again: "now" }} onRetry={() => {}} />);
    t.ok(out.includes(TRY), out);
    t.ok(out.includes('role="alert"'));
  },
  "a refusal (a 403) is said as what it is, with nothing to press": (t) => {
    const out = html(<LoadNote load={read({ error: "Only the administrator sets them.", refused: true })} />);
    t.ok(!out.includes(TRY), out);
    t.ok(out.includes('role="status"'), "not an alert: nothing went wrong");
    t.ok(out.includes("Only the administrator sets them."));
  },
  "a failure that named a wait offers nothing before it": (t) => {
    const waiting = html(<LoadNote load={read({ error: "Too many requests. Try again after 00:31 (in a minute).", waitUntil: NOW + 60_000 })} />);
    t.equal(count(waiting, TRY), 1, "only the sentence's own words, no button");
    t.ok(!waiting.includes("<button"), waiting);
    const over = html(<LoadNote load={read({ error: "Too many requests.", waitUntil: NOW - 1 })} />);
    t.ok(over.includes("<button"), "the wait is over");
  },
  "a page's reads that failed alike are said once, with what each one costs": (t) => {
    const same = "Too many requests. Try again later.";
    const reloaded: string[] = [];
    const out = html(
      <LoadNotes
        reads={[
          { load: read({ error: same, reload: () => void reloaded.push("list") }) },
          { load: read({ error: same, reload: () => void reloaded.push("events") }) },
          { load: read({ error: same }), more: "Without the projects, an endpoint can only be added for every project." },
          { load: read({}) },
        ]}
      />,
    );
    t.equal(count(out, 'role="alert"'), 1, out);
    t.equal(count(out, "Too many requests"), 1);
    t.ok(out.includes("Try again later. Without the projects"), "two sentences, not one cut into the other");
  },
  "reads that failed apart get a note each": (t) => {
    const out = html(<LoadNotes reads={[{ load: read({ error: "One." }) }, { load: read({ error: "Two." }) }]} />);
    t.equal(count(out, 'role="alert"'), 2);
  },
  "a page that throws leaves a note in its place, never a white screen": (t) => {
    t.deepEqual(PageBoundary.getDerivedStateFromError(new Error("WebGL 2 is not supported")), {
      broken: "WebGL 2 is not supported",
    });
    t.equal(messageOf("plain"), "plain");
    t.equal(messageOf(undefined), "unknown error");
    const b = new PageBoundary({ children: <p>the page</p> });
    t.ok(html(<>{b.render()}</>).includes("the page"), "draws its page while it works");
    b.state = { broken: "x.map is not a function" };
    const out = html(<>{b.render()}</>);
    t.ok(out.includes("This page could not be drawn."), out);
    t.ok(out.includes("x.map is not a function"));
    t.ok(!out.includes("the page"));
    t.ok(html(<PageBroke message="m" />).includes("Reload"));
  },
  "the graph asks the browser for WebGL 2 before it draws": (t) => {
    const doc = (ctx: unknown) => ({ createElement: () => ({ getContext: () => ctx }) }) as unknown as Document;
    t.equal(drawsWebGL2(doc(null)), false);
    let lost = 0;
    const gl = { getExtension: (n: string) => (n === "WEBGL_lose_context" ? { loseContext: () => void lost++ } : null) };
    t.equal(drawsWebGL2(doc(gl)), true);
    t.equal(lost, 1, "the probe's context is given back");
    t.equal(drawsWebGL2(doc({ getExtension: () => null })), true, "a browser without the extension still draws");
    t.equal(drawsWebGL2({ createElement: () => { throw new Error("no canvas"); } } as unknown as Document), false);
    t.match(NO_WEBGL2, /WebGL 2/);
  },
  "a locked sign-in says when, on the person's clock, never the server's ISO time": (t) => {
    const e = refusal(429, "too_many_attempts", "Try again after 2026-11-13T23:25:36.058348+00:00", {
      headers: { "retry-after": "899" },
    });
    const at = Math.ceil((NOW + 899_000) / 60_000) * 60_000;
    const said = loginFailure(e, NOW);
    t.equal(said, `Too many attempts. Try again ${afterWords(at, NOW)}.`);
    t.ok(!said.includes("T23:25"), said);
    t.equal(loginFailure(refusal(429, "too_many_attempts", "x"), NOW), "Too many attempts. Try again in a few minutes.");
  },
  "Add endpoint says what it waits for, in the form's order": (t) => {
    t.equal(whatAddWaitsFor({ url: " ", picked: 1, needsProjectChoice: true }), "Enter the endpoint's URL.");
    t.equal(whatAddWaitsFor({ url: "https://a", picked: 0, needsProjectChoice: true }), "Tick at least one event.");
    t.equal(whatAddWaitsFor({ url: "https://a", picked: 2, needsProjectChoice: true }), "Choose a project, or Every project.");
    t.equal(whatAddWaitsFor({ url: "https://a", picked: 2, needsProjectChoice: false }), null);
  },
};
