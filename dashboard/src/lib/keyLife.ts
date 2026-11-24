// A key's end as the API Keys page and Home say it, and the Rotate the page
// offers for it: the dashboard's half of the founder's decision of 2 Nov
// 2026 on keys, (a) rotation in one call with a while in which the old secret
// still works, the new one shown once, and (b) the end said to its holder
// (`Korely-Key-Expires-At` on every answer, since 1.0.23) and in a line on
// the dashboard. Until 1.0.26 only the API had them: the page showed a date,
// and a key was rotated by making another and revoking the first.
//
// What it does is a function of what it is handed (the key, the notice, the
// clock, the call), so keyLife.behaviour.tsx runs it in node.
import type { AgentKey, AgentKeyCreated, KeyLifetimes } from "@/lib/agentsApi";
import { failure, refusedByTheInstall } from "@/lib/apiError";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

/** A key's end near enough to say: when (milliseconds), how far in words,
 *  and whether it is the last day, said in hours and in red. */
export interface EndNear {
  at: number;
  words: string;
  lastDay: boolean;
}

/**
 * Whether `key` ends soon enough to say so: a key that rotates (a live key
 * a person made, `rotates` from the server) ending within `warningDays`,
 * the notice `key.expiring` gives the webhooks (GET /admin/keys/lifetimes,
 * `warning_days`), so the screen and the webhooks speak of one "soon". Its
 * last day is said apart: the decision's model, Anthropic, writes 7 days and
 * 1 day before a key ends
 * (https://platform.claude.com/docs/en/manage-claude/authentication, read 14
 * Nov 2026); the notice here starts earlier, with the webhooks'. Null for a
 * key with no end, one already past it (the page says "Expired"), a
 * connected app's (its secret renews itself every hour), and while the
 * notice is unread (`warningDays` null).
 */
export function endNear(
  key: Pick<AgentKey, "expires_at" | "rotates">,
  warningDays: number | null,
  now: number,
): EndNear | null {
  if (!key.rotates || !key.expires_at || warningDays === null) return null;
  const at = Date.parse(key.expires_at);
  if (Number.isNaN(at) || at <= now || at - now > warningDays * DAY) return null;
  return { at, words: inWords(at - now), lastDay: at - now < DAY };
}

/** How long `ms` is, said: "in 3 days", "in 1 day", "in 5 hours", "in under
 *  an hour". Rounded down, so it never says more time than is left. */
export function inWords(ms: number): string {
  if (ms >= DAY) {
    const d = Math.floor(ms / DAY);
    return `in ${d} day${d === 1 ? "" : "s"}`;
  }
  if (ms >= HOUR) {
    const h = Math.floor(ms / HOUR);
    return `in ${h} hour${h === 1 ? "" : "s"}`;
  }
  return "in under an hour";
}

/** The keys of `keys` whose end is near (`endNear`), the nearest first. */
export function endingSoon(
  keys: AgentKey[],
  warningDays: number | null,
  now: number,
): { key: AgentKey; near: EndNear }[] {
  const out: { key: AgentKey; near: EndNear }[] = [];
  for (const key of keys) {
    const near = endNear(key, warningDays, now);
    if (near) out.push({ key, near });
  }
  return out.sort((a, b) => a.near.at - b.near.at);
}

/** Until when the secret a rotation replaced still works (milliseconds), or
 *  null when none does. Stripe's rotated key shows the time left under the
 *  key's name (https://docs.stripe.com/keys, read 15 Nov 2026). */
export function oldSecretUntil(key: Pick<AgentKey, "old_secret_expires_at">, now: number): number | null {
  if (!key.old_secret_expires_at) return null;
  const at = Date.parse(key.old_secret_expires_at);
  return Number.isNaN(at) || at <= now ? null : at;
}

/** One choice of the Rotate dialog for the old secret: hours, or undefined
 *  for the install's default (the field not sent). */
export interface OverlapChoice {
  hours: number | undefined;
  label: string;
}

/** "Now" ends the old secret at once: what a key that leaked wants. 0 means
 *  that on every install, so it is offered even when the choices could not
 *  be read. */
export const NOW_CHOICE: OverlapChoice = { hours: 0, label: "Now" };

/**
 * What the Rotate dialog offers for the old secret, and what it starts on:
 * the server's choices and default (GET /admin/keys/lifetimes,
 * `old_secret_choices`, `old_secret_default_hours`), never a list written
 * here (the lifetimes' rule, audit of 17 Oct 2026). Unread, "Now" and the
 * install's default, which the dialog starts on.
 */
export function overlapChoices(lifetimes: KeyLifetimes | null): {
  choices: OverlapChoice[];
  start: number | undefined;
} {
  if (!lifetimes || !lifetimes.old_secret_choices?.length) {
    return { choices: [NOW_CHOICE, { hours: undefined, label: "This install's default" }], start: undefined };
  }
  const choices = lifetimes.old_secret_choices.map((c) => ({ hours: c.hours, label: c.label }));
  const start = choices.some((c) => c.hours === lifetimes.old_secret_default_hours)
    ? lifetimes.old_secret_default_hours
    : undefined;
  return { choices, start };
}

/** The choice in force in the Rotate dialog: what the person `picked`
 *  (null: nothing yet) while it is still offered, else where the offer
 *  starts. The choices can be read after the dialog opened: a pick of
 *  "Now" stays Now, and the install's default becomes the served default,
 *  so the menu never shows one choice while another is sent. */
export function overlapInForce(
  offer: { choices: OverlapChoice[]; start: number | undefined },
  picked: number | undefined | null,
): number | undefined {
  return picked !== null && offer.choices.some((c) => c.hours === picked) ? picked : offer.start;
}

/** The page's sentence when the install refused a rotation and said no
 *  reason. */
export const NOT_ROTATED = "The key was not rotated: its secret is the one it had. Try again.";

/** No answer came back, and the key, read again, lists the secret it had. */
export const NO_ANSWER_NOT_ROTATED =
  "No answer came back, but the key, read again, still has the secret it had: it was not rotated. Try again.";

/** No answer came back, and the key could not be read again. */
export const NO_ANSWER_UNKNOWN =
  'No answer came back, so the key may have been rotated and its new secret lost. Read this page again before rotating it: if the key then shows "Old secret works until", it was rotated, and rotating it again would end at once the secret your agents hold.';

/** No answer came back, the key was rotated, and its old secret has
 *  stopped too (Now): a rotation again loses nothing more. */
export const NO_ANSWER_ROTATED_NOW =
  "No answer came back, but the key was rotated: its new secret was lost on the way and cannot be shown again, and its old secret has stopped working. Rotate it again for a secret you can copy.";

/** What a rotation said: `again` false when Rotate must not be sent again
 *  from the dialog. */
export interface RotateTold {
  text: string;
  again: boolean;
}

/**
 * What the page says of a rotation that got no answer from the install,
 * from the key read again (`read`, null when that failed too) against the
 * key as the page listed it (`before`). The key's secret is rotated
 * exactly when the secret it lists changes: `masked` ends with the
 * secret's last four characters (api/admin.py `_key_out`), and an overlap
 * that starts gives it an `old_secret_expires_at` it did not have (the
 * two can repeat only together, the last four by a chance of one in 64^4).
 * Rotated with an overlap, the agents' secret works until that end, and a
 * rotation again ends it at once (services/keys.py, `reissue_secret`: one
 * old secret at a time), so the page says so and lets no Rotate through.
 */
export function afterNoAnswer(
  before: Pick<AgentKey, "id" | "masked" | "old_secret_expires_at">,
  read: AgentKey[] | null,
  now: number,
): RotateTold {
  const after = read?.find((k) => k.id === before.id);
  if (!after) return { text: NO_ANSWER_UNKNOWN, again: false };
  const overlapStarted =
    !!after.old_secret_expires_at && after.old_secret_expires_at !== before.old_secret_expires_at;
  if (after.masked === before.masked && !overlapStarted) {
    return { text: NO_ANSWER_NOT_ROTATED, again: true };
  }
  const until = oldSecretUntil(after, now);
  if (until === null) return { text: NO_ANSWER_ROTATED_NOW, again: true };
  return {
    text:
      "No answer came back, but the key was rotated: its new secret was lost on the way and cannot be shown again. " +
      `The secret your agents hold works until ${new Date(until).toLocaleString()}, and rotating again would end it at once: ` +
      "to switch them without a stop, give them a new key before then.",
    again: false,
  };
}

/**
 * Rotate `target` from the screen: `calls` are keysApi's rotate and list,
 * `hours` the overlap chosen. The new secret goes to `shown`, once.
 * Anything else goes to `failed`, and nothing is shown: a refusal of the
 * install's own (lib/apiError.ts `refusedByTheInstall`), said the screen's
 * one way (`failure`), changed nothing, since the route refuses before it
 * writes; with no answer from the install the key is read again and
 * `afterNoAnswer` says what happened. `held` (a ref of the dialog) lets
 * one rotation through at a time, read and set before the call: a second
 * click while the first is out would rotate again, and the secret the
 * first shows would be the old one, ending within its overlap (at once
 * with Now). It lets the next try through only where a rotation again
 * ends no secret an agent holds (`again`).
 */
export async function rotateFromTheScreen(
  calls: {
    rotate: (id: string, hours?: number) => Promise<AgentKeyCreated>;
    list: () => Promise<AgentKey[]>;
  },
  target: Pick<AgentKey, "id" | "masked" | "old_secret_expires_at">,
  hours: number | undefined,
  held: { current: boolean },
  said: { shown: (k: AgentKeyCreated) => void; failed: (text: string, again: boolean) => void },
  now: () => number = Date.now,
): Promise<void> {
  if (held.current) return;
  held.current = true;
  let made: AgentKeyCreated;
  try {
    made = await calls.rotate(target.id, hours);
  } catch (e) {
    let told: RotateTold;
    if (refusedByTheInstall(e)) {
      told = { text: failure(e, NOT_ROTATED, now()).text, again: true };
    } else {
      let read: AgentKey[] | null = null;
      try {
        read = await calls.list();
      } catch {
        // Not read either: what happened stays unknown (NO_ANSWER_UNKNOWN).
      }
      told = afterNoAnswer(target, read, now());
    }
    held.current = !told.again;
    said.failed(told.text, told.again);
    return;
  }
  said.shown(made);
}

/** What the dialog that shows a rotated key's new secret says of the old
 *  one: until when it works, or that it has stopped. */
export function afterRotation(k: Pick<AgentKey, "old_secret_expires_at">, now: number): string {
  const until = oldSecretUntil(k, now);
  if (until === null) return "The old secret has stopped working: only this one works now.";
  return `The old secret works until ${new Date(until).toLocaleString()}, then only this one does.`;
}
