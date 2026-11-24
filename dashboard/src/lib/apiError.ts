import { AxiosHeaders, isAxiosError } from "axios";

/**
 * The one way this screen reads a refusal from the server.
 *
 * Every error body this install sends carries `code` and `message` at the top
 * level, next to the `detail` it always sent (korely_agent/api/errors.py,
 * error_body): the endpoint's own code when it names one, the status's code
 * otherwise, and a sentence for a person, the first failed field of a 422
 * included.
 *
 * Until the audit of 17 Oct 2026 the dashboard read that body with two rules:
 * agentsApi.errorMessage took `detail` only when it was a string, so a 422 or
 * a `{code, message}` refusal showed the page's generic sentence, while
 * hosted.hostedError read the `message` the server sends every time. Both are
 * this function now. `detail` is still read when the top level is missing,
 * for a body that did not come through error_body.
 *
 * The unhandled 500 (`internal_error`, main.py) is not a refusal with a
 * reason: its message is "internal error", which tells a person less than the
 * page's own "nothing was changed, try again". It reads as no message, and the
 * page says its sentence.
 */
export interface ApiError {
  code: string;
  message: string;
}

/** The HTTP status of a failed call; null when no answer came back (the
 *  network, a stopped proxy), undefined when `e` is not a call's error. */
export function apiStatus(e: unknown): number | null | undefined {
  if (!isAxiosError(e)) return undefined;
  return e.response ? e.response.status : null;
}

export function apiError(e: unknown): ApiError | null {
  if (!isAxiosError(e)) return null;
  const data = e.response?.data;
  if (!data || typeof data !== "object") return null;
  let found: ApiError | null = null;
  if (typeof data.message === "string" && data.message) {
    found = { code: String(data.code ?? ""), message: data.message };
  } else {
    const detail = data.detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      found = { code: String(detail.code ?? ""), message: detail.message };
    } else if (typeof detail === "string" && detail) {
      found = { code: String(data.code ?? ""), message: detail };
    }
  }
  if (found?.code === "internal_error") return null;
  return found;
}

/** The refusal's `detail` when the server sent an object (a coded refusal
 *  with fields beside its message, such as `regions` of a 400
 *  `confirmation_required`); null otherwise. Read here, the one place that
 *  opens a call's error. */
export function apiErrorDetail(e: unknown): Record<string, unknown> | null {
  if (!isAxiosError(e)) return null;
  const detail = (e.response?.data as { detail?: unknown } | undefined)?.detail;
  return detail && typeof detail === "object" && !Array.isArray(detail)
    ? (detail as Record<string, unknown>)
    : null;
}

/** The seconds a refusal asks to wait before it is tried again: its
 *  `Retry-After`, either a number of seconds or an HTTP date (RFC 9110,
 *  10.2.3), the date counted from `now` (milliseconds). Null when the
 *  answer names no wait, or one that does not read. The server sends it on
 *  a 429 or a 503 (api/errors.py, a part's `*_spent`). */
export function apiRetryAfter(e: unknown, now: number): number | null {
  if (!isAxiosError(e) || !e.response) return null;
  const raw = AxiosHeaders.from(e.response.headers as Record<string, string>).get("retry-after");
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  if (/^[0-9]+$/.test(value)) return Math.min(Number(value), LONGEST_WAIT_S);
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.min(LONGEST_WAIT_S, Math.max(0, Math.ceil((at - now) / 1000)));
}

/** The longest wait the screen reads from a `Retry-After`: a year. The
 *  server's longest is a month (a plan's quota until the month ends); a
 *  larger number made a moment no Date can hold, said "Invalid Date"
 *  (the skeptic's finding of 14 Nov 2026, 99999999999999 seconds). */
export const LONGEST_WAIT_S = 366 * 24 * 60 * 60;

/** The longest delay a browser's setTimeout keeps: past it (24.8 days) the
 *  timer fires at once (HTML, timers: the delay is a signed 32-bit
 *  integer). */
export const LONGEST_TIMER_MS = 2 ** 31 - 1;

/** A clock and its timers, handed in so a wait runs without a browser
 *  (lib/behaviour.ts, FakeClock). */
export interface Clock {
  now: () => number;
  set: (fn: () => void, ms: number) => number;
  clear: (id: number) => void;
}

/** The browser's. */
export const BROWSER_CLOCK: Clock = {
  now: () => Date.now(),
  set: (fn, ms) => window.setTimeout(fn, ms),
  clear: (id) => window.clearTimeout(id),
};

/** Calls `fire` once at `moment` (milliseconds), however far: the wait is
 *  armed in steps no longer than a timer keeps. Returns a cancel. */
export function atMoment(moment: number, fire: () => void, clock: Clock): () => void {
  let id: number | null = null;
  const arm = () => {
    const left = moment - clock.now();
    if (left <= 0) {
      id = null;
      fire();
      return;
    }
    id = clock.set(arm, Math.min(left, LONGEST_TIMER_MS));
  };
  arm();
  return () => {
    if (id !== null) clock.clear(id);
  };
}

/** The page's sentence when no answer came back at all (`apiStatus` null). */
export const NOT_REACHED = "Could not reach this install. Is the service running?";

const MINUTE = 60_000;

/** When a refusal that names a wait (its `Retry-After`) may be tried
 *  again: that moment rounded up to the whole minute the page says, so a
 *  try at the minute said is not refused again. Null when it names none.
 *  `now` in milliseconds. Written first for one page (13 Nov 2026), and
 *  the one reader of a wait for the whole screen since the QA of 14 Nov
 *  2026: every page said "Too many requests" with "Try again", and the
 *  Retry-After the answer carried was never read. */
export function againAt(e: unknown, now: number): number | null {
  const seconds = apiRetryAfter(e, now);
  return seconds === null ? null : Math.ceil((now + seconds * 1000) / MINUTE) * MINUTE;
}

/** `at` in words for a person: the time on their clock, and how long from
 *  `now` ("after 14:32 (in 12 minutes)"). */
export function afterWords(at: number, now: number): string {
  const minutes = Math.max(1, Math.round((at - now) / MINUTE));
  const time = new Date(at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `after ${time} (in ${minutes === 1 ? "a minute" : `${minutes} minutes`})`;
}

/** The code a 2xx answer gets when its body is not data (lib/apiClient.ts):
 *  a web page, from something in front of the install (a proxy, a
 *  maintenance page) that answered in its place. */
export const NOT_DATA = "not_data";

/** What a page says of an answer that was not data: the read did not fail
 *  on the install, it never reached it (QA of 14 Nov 2026: a 200 with an
 *  HTML page emptied the whole screen). */
export const NOT_DATA_SAID =
  "The answer was a web page instead of data: something in front of this install (a proxy, a maintenance page) answered in its place.";

/** Whether `e` is the refusal lib/apiClient.ts makes of a 2xx answer that
 *  was not data. */
export function wasNotData(e: unknown): boolean {
  return isAxiosError(e) && e.code === NOT_DATA;
}

/** A failed call, said once for every page (QA of 14 Nov 2026): what to
 *  tell the person, and when trying again can help.
 *  - `again: "now"`: at will.
 *  - `again: "never"`: a 403, a refusal of who is asking that the same
 *    request with the same session cannot change (RFC 9110, 15.5.4): no
 *    "Try again" (Setup and Settings offered one to an operator who is not
 *    the install's administrator, forever).
 *  - `again: <ms>`: the answer named a wait (`Retry-After`, a 429 or a
 *    503): not before that moment, said in the text. */
export interface Failure {
  text: string;
  again: "now" | "never" | number;
}

/** `text` closed as a sentence, so another can follow it: the server's
 *  sentences end without a stop ("Too many requests"), and the page's
 *  next sentence ran into it ("Too many requests Without them..."). */
export function asSentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/u.test(t) ? t : `${t}.`;
}

/** `e` as a `Failure`: the server's sentence when it sent one, `fallback`
 *  otherwise; the wait it named, read with `apiRetryAfter` (`now` in
 *  milliseconds). */
export function failure(e: unknown, fallback: string, now: number): Failure {
  if (wasNotData(e)) return { text: NOT_DATA_SAID, again: "now" };
  const text = apiError(e)?.message ?? fallback;
  if (apiStatus(e) === 403) return { text, again: "never" };
  const at = againAt(e, now);
  if (at === null) return { text, again: "now" };
  return { text: `${asSentence(text)} Try again ${afterWords(at, now)}.`, again: at };
}

/** Whether the install itself refused `e`, with its reason (a body of
 *  api/errors.py, read by `apiError`). Then the request did not do what it
 *  asked, and a write may be sent again. Not so when no answer came back
 *  (the network, a timeout), when something in front of the install
 *  answered in its place (a proxy's 502 or 504, a web page: `wasNotData`),
 *  or for the unhandled 500, which can come after the write: whether the
 *  write happened is then not known, and a request whose method is not
 *  idempotent is not to be sent again blind (RFC 9110, 9.2.2: not "unless
 *  it has some means to detect that the original request was never
 *  applied"). Written for Rotate (the skeptic's finding of 15 Nov 2026: a
 *  rotation whose answer was lost said "not rotated, try again", and the
 *  try ended the secret the agents held). */
export function refusedByTheInstall(e: unknown): boolean {
  return !wasNotData(e) && apiError(e) !== null;
}
