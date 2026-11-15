import { Navigate, Outlet, useLocation } from "react-router";
import { useAuth } from "@/contexts/AuthContext";
import { FailureNote } from "@/components/LoadState";
import { gateOf } from "@/lib/session";

// The gate of every page behind the sign-in, in its own module so a check
// can draw it (lib/session.behaviour.ts) without the whole app.

/** Gate authenticated routes; bounce to /login while signed out. A
 *  session the server could not settle (any failure but a 401,
 *  lib/session.ts) is said here, with the session kept: it used to send the
 *  person to the sign-in page with nothing said (QA of 14 Nov 2026). */
export function RequireAuth() {
  const { user, loading, sessionFailure, checkSessionAgain, logout } = useAuth();
  const location = useLocation();
  switch (gateOf({ user, loading, sessionFailure })) {
    case "wait":
      return (
        <div className="flex h-full items-center justify-center text-muted-foreground">
          Loading…
        </div>
      );
    case "unsure":
      return (
        <div className="mx-auto flex h-full max-w-md flex-col justify-center gap-3 px-4">
          {sessionFailure && <FailureNote failure={sessionFailure} onRetry={checkSessionAgain} />}
          <button
            type="button"
            onClick={() => void logout()}
            className="self-start text-xs text-muted-foreground underline-offset-2 hover:underline"
          >
            Sign out of this browser instead
          </button>
        </div>
      );
    case "out":
      // Where they were going rides along, so the login brings them back to it.
      return <Navigate to="/login" replace state={{ from: location }} />;
    case "in":
      return <Outlet />;
  }
}
