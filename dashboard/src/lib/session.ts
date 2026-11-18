// Whether the session this browser keeps is still one, asked of the server
// when the screen loads: the one rule of both sign-ins (contexts/
// AuthContext.tsx on an install, hosted/auth.tsx on the Cloud).
//
// Only the server's 401 says the session is over. Until the QA of 14 Nov
// 2026 every failure of the read did: a 500, a 429, a 503, a network that
// dropped for a second (an install restarting) cleared the token and sent
// the person to the sign-in page with nothing said, to type a password that
// was right. Now any other failure keeps the token and is said, with a way
// to ask again (once a wait the answer named is over).
import { useCallback, useEffect, useRef, useState } from "react";
import { TOKEN_KEY } from "@/lib/apiClient";
import { NOT_REACHED, apiError, apiStatus, asSentence, failure, type Failure } from "@/lib/apiError";

/** What the read of the session said. `signedOut` carries what the sign-in
 *  page says, null when lib/apiClient.ts already made it say the session
 *  ended (a 401, `SESSION_ENDED`). */
export type SessionAnswer<T> =
  | { signedIn: T }
  | { signedOut: string | null }
  | { unsure: Failure };

/** What the screen says when it could not tell: the person is still signed
 *  in on this browser, and nothing was lost. */
export const SESSION_UNCHECKED = "Your session could not be checked.";

/** What the sign-in page says when the server answered that the session's
 *  account no longer exists (the Cloud's GET /hosted/account, 404
 *  `not_found`): the session is dead, and asking again would never change
 *  it. */
export const ACCOUNT_GONE = "The account you were signed in to no longer exists.";

/** The answers of a session's read that end the session, beyond the 401
 *  every read has: the read names them, from what its route documents. */
export type EndsIt = (status: number, code: string | null) => string | null;

/** The install's GET /admin/whoami: only its 401. */
export const ONLY_THE_401: EndsIt = () => null;

/** The Cloud's GET /hosted/account: its 404 `not_found` too, "the
 *  session's account no longer exists" (hosted/api_identity.account). */
export const OR_THE_ACCOUNT_GONE: EndsIt = (status, code) =>
  status === 404 && code === "not_found" ? ACCOUNT_GONE : null;

/** What the read `read` (GET /admin/whoami, or the Cloud's account) says of
 *  the session: signed in with its answer; signed out on a 401 (the token
 *  is cleared by lib/apiClient.ts, which sees every 401) or on an answer
 *  the read says ends it (`endsIt`); unsure on anything else, with the
 *  failure said the screen's one way. `now` in milliseconds. */
export async function readSession<T>(
  read: () => Promise<T>,
  now: () => number,
  endsIt: EndsIt = ONLY_THE_401,
): Promise<SessionAnswer<T>> {
  try {
    return { signedIn: await read() };
  } catch (e) {
    const status = apiStatus(e);
    if (status === 401) return { signedOut: null };
    if (typeof status === "number") {
      const gone = endsIt(status, apiError(e)?.code || null);
      if (gone !== null) return { signedOut: gone };
    }
    // A network that dropped has no answer to quote: the install was not
    // reached, which is the useful thing to say. An error that is not a
    // call's (`apiStatus` undefined) happened in this browser.
    const fallback =
      status === null
        ? NOT_REACHED
        : status === undefined
          ? "The check failed in this browser: reload the page."
          : `The install could not answer (HTTP ${status}).`;
    const said = failure(e, fallback, now());
    return {
      unsure: { text: `${SESSION_UNCHECKED} ${asSentence(said.text)} You are still signed in on this browser.`, again: said.again },
    };
  }
}

/** How a sign-in provider signs a person in with the read's answer, and out
 *  (`said`: what the sign-in page says, null for nothing more). The rest
 *  of the check is here, the same for both providers. */
export interface SessionKeeper<T> {
  signIn: (value: T) => void;
  signOut: (said: string | null) => void;
}

/** What the screen shows of the check: still asking, and why it could not
 *  tell (null: it could, or it did not ask). */
export interface SessionShown {
  loading: boolean;
  failure: Failure | null;
}

/** Asks the server about the session this browser keeps, if it keeps one
 *  (`hasToken`), and hands each answer to the provider (`keeper`) and to
 *  the screen (`show`): signed in or out ends the asking with no failure;
 *  any other answer keeps the session and shows why. Returns a cancel: a
 *  provider that left takes no answer. Until the QA of 14 Nov 2026 each
 *  provider wrote these steps itself, and both signed out on any failure. */
export function askAboutTheSession<T>(
  read: () => Promise<T>,
  keeper: SessionKeeper<T>,
  show: (s: SessionShown) => void,
  hasToken: () => boolean,
  endsIt: EndsIt = ONLY_THE_401,
  now: () => number = Date.now,
): () => void {
  if (!hasToken()) {
    show({ loading: false, failure: null });
    return () => {};
  }
  show({ loading: true, failure: null });
  let cancelled = false;
  void readSession(read, now, endsIt).then((a) => {
    if (cancelled) return;
    if ("signedIn" in a) keeper.signIn(a.signedIn);
    else if ("signedOut" in a) keeper.signOut(a.signedOut);
    show({ loading: false, failure: "unsure" in a ? a.unsure : null });
  });
  return () => {
    cancelled = true;
  };
}

/** The check, for a provider: what the screen shows of it, a way to ask
 *  again, and `forget` for a sign-out or an ended session that leaves the
 *  failure behind. `read`, `keeper` and `endsIt` are read when it asks. */
export function useSessionCheck<T>(read: () => Promise<T>, keeper: SessionKeeper<T>, endsIt: EndsIt = ONLY_THE_401) {
  const [shown, setShown] = useState<SessionShown>({ loading: true, failure: null });
  const [attempt, setAttempt] = useState(0);
  const latest = useRef({ read, keeper, endsIt });
  latest.current = { read, keeper, endsIt };
  useEffect(
    () =>
      askAboutTheSession(
        () => latest.current.read(),
        { signIn: (v) => latest.current.keeper.signIn(v), signOut: (said) => latest.current.keeper.signOut(said) },
        setShown,
        () => localStorage.getItem(TOKEN_KEY) !== null,
        latest.current.endsIt,
      ),
    [attempt],
  );
  const checkSessionAgain = useCallback(() => setAttempt((n) => n + 1), []);
  const forget = useCallback(() => setShown((s) => ({ ...s, failure: null })), []);
  return { loading: shown.loading, sessionFailure: shown.failure, checkSessionAgain, forget };
}

/** What a page behind the sign-in shows: wait, the reason the session could
 *  not be checked (the session kept), the page, or the sign-in page. */
export type Gate = "wait" | "unsure" | "in" | "out";

export function gateOf(a: { user: unknown; loading: boolean; sessionFailure: Failure | null }): Gate {
  if (a.loading) return "wait";
  if (a.user) return "in";
  return a.sessionFailure ? "unsure" : "out";
}
