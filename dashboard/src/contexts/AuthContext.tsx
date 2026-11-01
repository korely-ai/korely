/**
 * Who is logged in, on a private install.
 *
 * The version this replaces used Firebase: three OAuth providers, a popup, and
 * an exchange that turned a Google token into one of ours. None of that
 * belongs on a machine a customer owns and runs. There is no identity provider
 * here and there should not be one: this install authenticates a person with a
 * username and a password against its own database, and a customer who wants
 * SSO puts their own gateway in front and sets TRUST_AUTH_HEADER.
 *
 * The context kept its shape through that swap (`user`, `loading`,
 * `authError`, `login`, `logout`), because `apiClient` already took a token
 * out of localStorage without caring where it came from. The person it holds
 * lost Firebase's shape later (audit of 17 Oct 2026): see `Operator`.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { apiError, apiStatus } from "@/lib/apiError";

import apiClient, { TOKEN_KEY } from "@/lib/apiClient";

export { TOKEN_KEY };

/**
 * Who is signed in. `name` is the username on an install and the email on the
 * Cloud; `signIn` is how (a password, or the Cloud's provider). It was
 * Firebase's shape, `email` and `providerData[0].providerId`, kept after
 * Firebase left the install: Settings then labelled an install's username
 * "Email" (audit of 17 Oct 2026).
 */
export interface Operator {
  name: string;
  uid: string;
  signIn: string;
}

export interface AuthContextValue {
  user: Operator | null;
  loading: boolean;
  authError: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** End every session of this operator, this one included. */
  logoutEverywhere: () => Promise<void>;
}

// Exported for the hosted service's provider (dashboard/src/hosted/auth.tsx),
// which fills the same shape after a sign-in with Google, Microsoft, GitHub
// or an email: every page reads `useAuth` and never knows which.
export const AuthContext = createContext<AuthContextValue | undefined>(undefined);

// A copy of the signed-in person this browser kept, and nothing ever read
// back: the server is asked on every load. No longer written; a sign-out still
// removes the copy an earlier version left (audit of 17 Oct 2026).
const OLD_WHO_KEY = "korely_operator";

/**
 * Why a sign-in failed, in one sentence. Only the server's own refusal,
 * `invalid_credentials` (POST /admin/session), is a wrong password: until the
 * audit of 17 Oct 2026 every failure but a 429 said so, a stopped API behind a
 * running proxy (502) and a network error included, and sent the operator to
 * reset a password that was right.
 */
export function loginFailure(e: unknown): string {
  const status = apiStatus(e);
  if (status === undefined) return "The sign-in failed in this browser. Reload the page and try again.";
  if (status === null) return "Could not reach this install. Is the service running?";
  const refusal = apiError(e);
  // 429 carries when the lockout ends, and that sentence is the only
  // useful thing anybody can be told at that moment.
  if (status === 429) return refusal?.message ?? "Too many attempts. Try again shortly.";
  if (refusal?.code === "invalid_credentials") return "Wrong username or password.";
  return (
    `The install could not answer the sign-in (HTTP ${status}): this is not a ` +
    "wrong password. Try again in a moment."
  );
}

/** What the sign-in page says when the server ended the session in use (it
 *  expired, or was signed out from elsewhere): the next click landed on the
 *  sign-in page with nothing said (functional test of 28 Oct 2026). */
export const SESSION_ENDED = "Your session ended, sign in again.";

/** Whether an `auth:session-expired` (lib/apiClient.ts) ended a session
 *  this browser held. */
export function endedASession(e: Event): boolean {
  return (e as CustomEvent<{ hadSession?: boolean }>).detail?.hadSession === true;
}

export function clear() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(OLD_WHO_KEY);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Operator | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);

  /**
   * On load, the token in localStorage is a claim, not a fact: it may have
   * expired, or been revoked from the command line while the tab was closed.
   * Asking the server settles it before any page renders as logged in.
   */
  useEffect(() => {
    let cancelled = false;
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) {
      setLoading(false);
      return;
    }
    apiClient
      .get("/admin/whoami")
      .then((r) => {
        if (cancelled) return;
        setUser({ name: r.data.name, uid: r.data.tenant_id, signIn: r.data.kind });
      })
      .catch(() => {
        if (cancelled) return;
        clear();
        setUser(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** A session expiring mid-use is the same thing as logging out, said. */
  useEffect(() => {
    const onExpired = (e: Event) => {
      clear();
      setUser(null);
      if (endedASession(e)) setAuthError(SESSION_ENDED);
    };
    window.addEventListener("auth:session-expired", onExpired);
    return () => window.removeEventListener("auth:session-expired", onExpired);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setAuthError(null);
    try {
      const r = await apiClient.post("/admin/session", { username, password });
      localStorage.setItem(TOKEN_KEY, r.data.token);
      setUser({ name: r.data.username, uid: r.data.tenant_id, signIn: "password" });
    } catch (e: unknown) {
      setAuthError(loginFailure(e));
      throw e;
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiClient.delete("/admin/session");
    } catch {
      // The token is going away here regardless: a logout that fails because
      // the server is unreachable must still log you out of this browser.
    }
    clear();
    setUser(null);
  }, []);

  // Unlike `logout`, a failure here is said, not swallowed: the person asked
  // for the other devices too, and only the server can sign those out.
  const logoutEverywhere = useCallback(async () => {
    await apiClient.delete("/admin/sessions");
    clear();
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, authError, login, logout, logoutEverywhere }),
    [user, loading, authError, login, logout, logoutEverywhere],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
