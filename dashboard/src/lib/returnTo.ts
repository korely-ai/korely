import type { Location } from "react-router";

/** Where a login sends the person back: the page that sent them to /login
 *  (RequireAuth puts it in the navigation state), else the home page. The
 *  OAuth consent page relies on it: somebody who arrives from ChatGPT or
 *  Claude signs in and lands on the consent, with its request id, not on a
 *  list. The two logins used to fall back to different pages, the install's
 *  to /keys and the Cloud's to / (audit of 17 Oct 2026); the home page hands
 *  a first key to whoever has none. */
export function returnTarget(state: unknown, fallback = "/"): string {
  const from = (state as { from?: Location } | null)?.from;
  return from && from.pathname !== "/login" ? `${from.pathname}${from.search}` : fallback;
}
