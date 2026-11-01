import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import {
  endUsersFrom,
  errorMessage,
  keysApi,
  oauthApi,
  projectsApi,
  type AgentProject,
  type OAuthRequest,
} from "@/lib/agentsApi";

/**
 * An app asks to connect to this memory (OAuth, api/oauth.py, 16 Oct 2026):
 * ChatGPT or Claude sent the browser here with a request id, after the
 * person signed in. They choose the project the app may use and whether it
 * may write; the app then receives a key of that project, which shows in API
 * Keys under the app's name and is revoked there like any key.
 */
// What each scope opens, in the server's words (GET /admin/scopes, the ones
// API Keys shows): this page kept its own copy (audit of 17 Oct 2026).

// Where the browser may go back to. The server checks it when the app
// registers (services/oauth.py, redirect_allowed); checked again here because
// this page navigates to it, and a javascript: address would run in the
// dashboard with the person's session.
function safeRedirect(target: string): boolean {
  try {
    const u = new URL(target);
    if (u.protocol === "https:") return true;
    if (u.protocol === "http:") return ["127.0.0.1", "[::1]", "localhost"].includes(u.hostname);
    return u.protocol.slice(0, -1).includes(".");
  } catch {
    return false;
  }
}

// Shown inside another site's frame, this page could have its Allow clicked
// under a decoy (clickjacking): the server sends X-Frame-Options and CSP
// frame-ancestors on every screen (api/spa.py), and the page refuses on its
// own too, for a proxy or a host that serves it without them (audit of 18 Oct
// 2026).
function framed(): boolean {
  try {
    return window.top !== window.self;
  } catch {
    return true;
  }
}

export function OAuthConsentPage() {
  const [params] = useSearchParams();
  const requestId = params.get("request") ?? "";
  const [request, setRequest] = useState<OAuthRequest | null>(null);
  const [projects, setProjects] = useState<AgentProject[]>([]);
  const [projectId, setProjectId] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [opens, setOpens] = useState<Record<string, string>>({});
  // Confined to some end users of the project (services/user_scope.py).
  const [users, setUsers] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (framed()) {
      setError("This page cannot be answered inside another site. Open it in its own tab.");
      return;
    }
    if (!requestId) {
      setError("This page needs a request from an app. Start again from the app.");
      return;
    }
    // The scopes' words are a help, not the request: unread, the names show.
    const known = keysApi.scopes().catch(() => ({ scopes: [], default: [] }));
    Promise.all([oauthApi.show(requestId), projectsApi.list(), known])
      .then(([req, list, known]) => {
        setRequest(req);
        setOpens(Object.fromEntries(known.scopes.map((sc) => [sc.name, sc.opens])));
        setScopes(req.scopes);
        const live = list.projects.filter((p) => !p.archived);
        setProjects(live);
        setProjectId((live.find((p) => p.is_default) ?? live[0])?.id ?? "");
      })
      .catch((e) => setError(errorMessage(e) ?? "This request cannot be read."));
  }, [requestId]);

  const answer = async (yes: boolean) => {
    if (busy || framed()) return;
    setBusy(true);
    try {
      const { redirect } = yes
        ? await oauthApi.approve(requestId, projectId, scopes, endUsersFrom(users))
        : await oauthApi.deny(requestId);
      if (!safeRedirect(redirect)) {
        setError("This app's return address is not allowed.");
        setBusy(false);
        return;
      }
      window.location.assign(redirect);
    } catch (e) {
      setError(errorMessage(e) ?? "The answer was not saved.");
      setBusy(false);
    }
  };

  const toggle = (s: string) =>
    setScopes((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0B0F17] px-4">
      <div className="w-full max-w-[420px]">
        <h1 className="text-[#E2E8ED] text-2xl font-semibold mb-1">
          {request ? `Connect ${request.client_name}` : "Connect an app"}
        </h1>
        {request && (
          <p className="text-[#9CA3AF] text-sm mb-6">
            {request.client_name} asks to use your Korely memory.{" "}
            {request.redirect_host
              ? `After you answer, you go back to ${request.redirect_host}.`
              : `After you answer, the answer goes to the app on this device that opens ` +
                `"${request.redirect_app_scheme}:" links, not to a website.`}
          </p>
        )}

        {error && (
          <p role="alert" className="text-[13px] text-[#F87171] mb-4">
            {error}
          </p>
        )}

        {request && (
          <div className="space-y-5">
            <div>
              <label htmlFor="project" className="block text-[13px] text-[#9CA3AF] mb-1.5">
                Project
              </label>
              <select
                id="project"
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
                className="w-full rounded-lg bg-white/4 border border-white/10 px-3 py-2.5 text-[#E2E8ED] text-[15px] outline-hidden focus:border-[#3B82F6]/60"
              >
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-[12px] text-[#6B7280]">
                The app reads and writes this project only.
              </p>
            </div>

            <fieldset className="space-y-2">
              <legend className="block text-[13px] text-[#9CA3AF] mb-1.5">Permissions</legend>
              {request.scopes.map((s) => (
                <label key={s} className="flex items-start gap-2 text-[14px] text-[#E2E8ED]">
                  <input
                    type="checkbox"
                    checked={scopes.includes(s)}
                    onChange={() => toggle(s)}
                    className="mt-1"
                  />
                  <span>
                    <span className="font-mono text-[13px]">{s}</span>
                    {opens[s] && (
                      <span className="block text-[12px] text-[#9CA3AF]">{opens[s]}</span>
                    )}
                  </span>
                </label>
              ))}
              <p className="text-[12px] text-[#6B7280]">
                The app stays connected while it keeps renewing its access. It is
                disconnected when you revoke its key in API Keys, when it has not
                renewed for 90 days, when this project is deleted, or when the
                memory is restored from a backup.
              </p>
            </fieldset>

            <div>
              <label htmlFor="users" className="block text-[13px] text-[#9CA3AF] mb-1.5">
                Only these end users (optional)
              </label>
              <input
                id="users"
                value={users}
                onChange={(e) => setUsers(e.target.value)}
                placeholder="e.g. founder, repo:*"
                className="w-full rounded-lg bg-white/4 border border-white/10 px-3 py-2.5 text-[#E2E8ED] text-[15px] font-mono outline-hidden focus:border-[#3B82F6]/60"
              />
              <p className="mt-1.5 text-[12px] text-[#6B7280]">
                Empty: the whole project. A trailing * is a prefix.
              </p>
            </div>

            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => answer(true)}
                disabled={busy || !projectId || scopes.length === 0}
                className="flex-1 rounded-lg bg-[#3B82F6] hover:bg-[#2563EB] disabled:opacity-40 disabled:hover:bg-[#3B82F6] text-white text-[15px] font-medium py-2.5 transition-colors"
              >
                Allow
              </button>
              <button
                type="button"
                onClick={() => answer(false)}
                disabled={busy}
                className="flex-1 rounded-lg border border-white/10 hover:bg-white/5 disabled:opacity-40 text-[#E2E8ED] text-[15px] font-medium py-2.5 transition-colors"
              >
                Deny
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
