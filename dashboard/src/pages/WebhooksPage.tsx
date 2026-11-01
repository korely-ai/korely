import { useEffect, useMemo, useState } from "react";
import {
  Webhook as WebhookIcon,
  Trash2,
  Send,
  Power,
  ChevronDown,
  ChevronRight,
} from "lucide-react";
import { PageHeader, EmptyState, HelpNote, fieldClass } from "@/components/PageShell";
import {
  errorMessage,
  webhooksApi,
  type Webhook,
} from "@/lib/agentsApi";
import { useLoad } from "@/lib/useLoad";
import { useProjects } from "@/contexts/ProjectContext";
import { ErrorNote, Loaded } from "@/components/LoadState";
import { CopyButton } from "@/components/CopyButton";
import { DOCS } from "@/lib/docsLinks";

// The events offered are the ones the server says it sends
// (GET /admin/webhooks/events, services/webhooks.py VALID_EVENTS): a list
// written here once offered an event that never fired and missed two that did,
// so unticking the dead one quietly unsubscribed from key expiry.

const PAYLOAD_EXAMPLE = `POST https://your-app.com/webhooks/korely
webhook-id: msg_2aXk…
webhook-timestamp: 1749725400
webhook-signature: v1,k8sH3p…

{"data":{"id":"mem_1e51…","user_id":"maria","agent_id":null},"event":"memory.created",
 "project_id":"7c0f…"}`;

// Standard Webhooks: the HMAC key is the base64-DECODED part of the secret
// after "whsec_", not the string itself. These used the string, like the
// server did, so the two agreed with each other and with no official library.
// tests/test_webhook_signatures.py runs the Python one on a real delivery,
// and the JavaScript one in node where node is installed; everywhere, it
// checks what the JavaScript one says against the server's signer.
const VERIFY_PY = `# Standard Webhooks: HMAC-SHA256 over "{id}.{timestamp}.{body}", keyed by the
# base64-decoded part of the secret after "whsec_". The official library does
# this and also rejects stale timestamps: pip install standardwebhooks
import hmac, hashlib, base64

def verify(secret: str, headers, raw_body: str) -> bool:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f'{headers["webhook-id"]}.{headers["webhook-timestamp"]}.{raw_body}'
    expected = "v1," + base64.b64encode(
        hmac.new(key, signed.encode(), hashlib.sha256).digest()
    ).decode()
    # the header can carry several signatures, separated by spaces
    return any(hmac.compare_digest(expected, s)
               for s in headers["webhook-signature"].split(" "))`;

const VERIFY_JS = `// Standard Webhooks: HMAC-SHA256 over \`\${id}.\${timestamp}.\${body}\`, keyed by the
// base64-decoded part of the secret after "whsec_". The official library does
// this and also rejects stale timestamps: npm install standardwebhooks
import crypto from "crypto";

function verify(secret, headers, rawBody) {
  const key = Buffer.from(secret.slice("whsec_".length), "base64");
  const signed = \`\${headers["webhook-id"]}.\${headers["webhook-timestamp"]}.\${rawBody}\`;
  const expected =
    "v1," + crypto.createHmac("sha256", key).update(signed).digest("base64");
  // the header can carry several signatures, separated by spaces
  return headers["webhook-signature"]
    .split(" ")
    .some((s) => s.length === expected.length &&
      crypto.timingSafeEqual(Buffer.from(s), Buffer.from(expected)));
}`;

// What "Send a test" answered: `text` is the receiver's status or error, or
// the server's refusal. It used to say "sent ✓" for any 200, the receiver's
// own 500 included, and "failed" for everything else (audit of 17 Oct 2026).
type TestState = { state: "sending" | "ok" | "err"; text?: string };

export function WebhooksPage() {
  // The reads of this page (lib/useLoad.ts). The events and the projects
  // used to fail into an empty list with nothing said: no boxes to tick, Add
  // disabled, and every bound endpoint named "a deleted project" (audit of
  // 17 Oct 2026).
  const listing = useLoad(() => webhooksApi.list(), [], "Could not load webhooks.");
  const eventsRead = useLoad(
    () => webhooksApi.events(),
    [],
    "The events this server sends could not be read.",
  );
  const events = eventsRead.data ?? [];
  // The project whose events the endpoint receives, "" for every project
  // (services/webhooks.py, 8 Oct 2026): a staging receiver need not get
  // production's end users. The projects are the app's one list
  // (contexts/ProjectContext.tsx): this page read a copy of its own, so a
  // project renamed or deleted on the Projects page kept its old name here,
  // and a new one was not offered, until a reload (audit of 17 Oct
  // 2026). Read again on arrival, as the copy was: another tab may have
  // changed it.
  const { projects: allProjects, projectsLoad, refresh: refreshProjects } = useProjects();
  useEffect(() => {
    refreshProjects();
  }, [refreshProjects]);
  const projects = useMemo(() => allProjects.filter((p) => !p.archived), [allProjects]);
  const [error, setError] = useState<string | null>(null);

  const [url, setUrl] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  useEffect(() => {
    if (eventsRead.data) setPicked(eventsRead.data);
  }, [eventsRead.data]);
  const [projectId, setProjectId] = useState("");
  const [creating, setCreating] = useState(false);

  const [helpOpen, setHelpOpen] = useState(false);
  const [lang, setLang] = useState<"python" | "node">("python");
  const [tests, setTests] = useState<Record<string, TestState | undefined>>({});

  // The secret comes back exactly once, in the response to the create call, and
  // never in a listing. This page used to await that response, throw it away,
  // and reload the list — so through the screen the secret was never shown at
  // all, while the row underneath claimed it had been "shown once". Anyone who
  // only used the dashboard could not verify a single delivery signature. Kept
  // here, in memory, for as long as the page is open: not in storage, because
  // it is the key to forging a delivery.
  const [freshSecrets, setFreshSecrets] = useState<Record<string, string>>({});

  const load = listing.reload;
  const withSecret = (r: Webhook): Webhook =>
    r.secret || !freshSecrets[r.id] ? r : { ...r, secret: freshSecrets[r.id] };
  // "a deleted project" only when the projects were read and it is not there.
  const projectName = (id: string | null) =>
    allProjects.find((p) => p.id === id)?.name ??
    (projectsLoad.data ? "a deleted project" : "one project");

  const toggle = (e: string) =>
    setPicked((p) => (p.includes(e) ? p.filter((x) => x !== e) : [...p, e]));

  const create = async () => {
    const u = url.trim();
    // Nothing ticked is not "everything": it used to be sent as "*". The
    // button is disabled for it, and the server refuses an empty list anyway.
    if (!u || picked.length === 0) return;
    setCreating(true);
    setError(null);
    try {
      const subscribed = picked.length === events.length ? ["*"] : picked;
      const made = await webhooksApi.create(u, subscribed, projectId || null);
      if (made?.secret) {
        setFreshSecrets((m) => ({ ...m, [made.id]: made.secret as string }));
      }
      setUrl("");
      setPicked([...events]);
      setProjectId("");
      load();
    } catch (e) {
      // The server says which: an address it will not connect to, or events
      // it does not send.
      setError(errorMessage(e) ?? "Could not create the endpoint. Check the URL.");
    } finally {
      setCreating(false);
    }
  };

  // A delete that failed said nothing: the row stayed and the reason went
  // to the console. It also asked nothing, and it takes the signing secret
  // with it (audit of 17 Oct 2026).
  const remove = async (h: Webhook) => {
    if (
      !confirm(
        `Delete the endpoint ${h.url}? Nothing more is sent to it, and its signing ` +
          `secret goes with it: an endpoint made again gets a new one.`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await webhooksApi.remove(h.id);
    } catch (e) {
      setError(errorMessage(e) ?? "The endpoint was not deleted. Try again.");
      return;
    }
    load();
  };

  const enable = async (id: string) => {
    setError(null);
    try {
      await webhooksApi.enable(id);
    } catch (e) {
      // A URL that no longer passes the check is refused with the reason.
      setError(errorMessage(e) ?? "The endpoint was not switched back on. Try again.");
      return;
    }
    load();
  };

  const sendTest = async (id: string) => {
    setTests((t) => ({ ...t, [id]: { state: "sending" } }));
    let result: TestState;
    try {
      const out = await webhooksApi.test(id);
      result = out.delivered
        ? { state: "ok", text: out.status ? `sent, ${out.status}` : "sent" }
        : {
            state: "err",
            text: out.status ? `your server answered ${out.status}` : out.error ?? "not delivered",
          };
    } catch (e) {
      result = { state: "err", text: errorMessage(e) ?? "the test did not run" };
    }
    setTests((t) => ({ ...t, [id]: result }));
    // A success clears itself; a failure stays until the next test, it is
    // something to go and fix.
    if (result.state === "ok") {
      setTimeout(() => setTests((t) => ({ ...t, [id]: undefined })), 2500);
    }
  };

  return (
    <div>
      <PageHeader
        title="Webhooks"
        subtitle="Get a signed POST when a memory is created, a fact is superseded, or an API key is about to expire."
        docs={DOCS.webhooks}
      />

      <HelpNote title="What webhooks are for" defaultOpen>
        <p>
          Instead of your app polling us asking "did anything change?", a webhook
          is <span className="text-foreground">Korely calling your server</span>{" "}
          the moment something happens: a memory created, a fact superseded, or
          an API key about to expire. You give us a URL; we POST a small signed JSON to
          it in real time. Add an endpoint below, then expand "How to receive &amp;
          verify events" for the payload shape and a copy-paste verification
          snippet.
        </p>
      </HelpNote>

      {/* how it works */}
      <div className="mb-6 rounded-lg border border-border bg-card">
        <button
          type="button"
          onClick={() => setHelpOpen((o) => !o)}
          className="flex w-full items-center gap-2 px-5 py-3 text-left text-sm font-medium"
        >
          {helpOpen ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          How to receive &amp; verify events
        </button>

        {helpOpen && (
          <div className="border-t border-border px-5 py-4 text-sm">
            <ol className="mb-4 list-decimal space-y-1.5 pl-5 text-muted-foreground">
              <li>We <span className="text-foreground">POST</span> a JSON body to your URL the moment an event fires.</li>
              <li>Three headers carry the <a href="https://www.standardwebhooks.com" target="_blank" rel="noreferrer" className="text-foreground underline underline-offset-2">Standard Webhooks</a> signature: <code className="font-mono text-xs">webhook-id</code>, <code className="font-mono text-xs">webhook-timestamp</code>, <code className="font-mono text-xs">webhook-signature</code>.</li>
              <li>Verify the signature with that endpoint's <span className="text-foreground">signing secret</span> (shown once, when the endpoint is created) before trusting the body. The HMAC key is the base64-decoded part after <code className="font-mono text-xs">whsec_</code>, as the spec says, so the official <code className="font-mono text-xs">standardwebhooks</code> library works as it is.</li>
              <li><span className="text-foreground">Send a test</span> posts the same envelope, signed the same way, with <code className="font-mono text-xs">"event":"ping"</code> and <code className="font-mono text-xs">"test":true</code>: acknowledge it like any event you do not act on.</li>
              <li>Return any <span className="text-foreground">2xx</span> to acknowledge. Non-2xx (or a timeout) is retried with backoff; a delivery that spends its attempts disables the endpoint, and <span className="text-foreground">Enable</span> on its row switches it back on with the same URL and secret.</li>
            </ol>

            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              What a delivery looks like
            </p>
            <pre className="mb-4 overflow-x-auto rounded-md border border-border bg-background px-3 py-2.5 text-[11px] leading-relaxed">
              {PAYLOAD_EXAMPLE}
            </pre>

            <div className="mb-1.5 flex items-center justify-between">
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                Verify the signature
              </p>
              <div className="flex gap-1">
                {(["python", "node"] as const).map((l) => (
                  <button
                    key={l}
                    type="button"
                    onClick={() => setLang(l)}
                    className={`rounded px-2 py-0.5 text-xs ${
                      lang === l
                        ? "bg-secondary text-foreground"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {l === "python" ? "Python" : "Node"}
                  </button>
                ))}
              </div>
            </div>
            <div className="relative">
              <CopyButton
                text={lang === "python" ? VERIFY_PY : VERIFY_JS}
                label="Copy the snippet"
                className="absolute right-2 top-2 rounded bg-background p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"
              />
              <pre className="overflow-x-auto rounded-md border border-border bg-background px-3 py-2.5 text-[11px] leading-relaxed">
                {lang === "python" ? VERIFY_PY : VERIFY_JS}
              </pre>
            </div>
          </div>
        )}
      </div>

      {/* add endpoint */}
      <div className="mb-6 rounded-lg border border-border bg-card p-5">
        <p className="mb-3 text-sm font-medium">Add an endpoint</p>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://your-app.com/webhooks/korely"
          className={`mb-3 w-full ${fieldClass}`}
        />
        {eventsRead.error && (
          <ErrorNote onRetry={eventsRead.reload} className="mb-3">
            {eventsRead.error}
          </ErrorNote>
        )}
        {projectsLoad.error && (
          <ErrorNote onRetry={refreshProjects} className="mb-3">
            {projectsLoad.error} Without them an endpoint can only be added for every project.
          </ErrorNote>
        )}
        <div className="mb-3 flex flex-wrap gap-3">
          {events.map((e) => (
            <label key={e} className="flex cursor-pointer items-center gap-1.5 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={picked.includes(e)}
                onChange={() => toggle(e)}
                className="h-4 w-4 rounded border-input"
              />
              <span className="font-mono text-xs">{e}</span>
            </label>
          ))}
        </div>
        {projects.length > 1 && (
          <div className="mb-3">
            <select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className={`w-full ${fieldClass}`}
            >
              <option value="">Every project</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  Only {p.name}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-muted-foreground">
              Bound to a project, it receives that project's events and nothing
              from the others. Deleting the project deletes the endpoint.
            </p>
          </div>
        )}
        <button
          type="button"
          onClick={create}
          disabled={creating || !url.trim() || picked.length === 0}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {creating ? "Adding…" : "Add endpoint"}
        </button>
      </div>

      {error && (
        <p className="mb-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}

      <Loaded load={listing}>
        {(rows) => rows.length === 0 ? (
        <EmptyState icon={<WebhookIcon className="h-6 w-6" />} title="No endpoints yet">
          Add a URL above and Korely will POST signed events to it. Click "How
          to receive &amp; verify events" for the payload shape and a copy-paste
          verification snippet.
        </EmptyState>
      ) : (
        <ul className="space-y-3">
          {rows.map(withSecret).map((h) => {
            const ts = tests[h.id];
            return (
              <li
                key={h.id}
                className={`rounded-lg border bg-card px-4 py-3 ${
                  h.active ? "border-border" : "border-destructive/40"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className={`truncate font-mono text-sm ${h.active ? "" : "text-muted-foreground"}`}>
                      {h.url}
                    </p>
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                      {!h.active && (
                        <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[11px] font-medium text-destructive">
                          Disabled: nothing is sent to it
                        </span>
                      )}
                      {h.project_id && (
                        <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[11px] text-primary">
                          Only {projectName(h.project_id)}
                        </span>
                      )}
                      {(h.events || []).map((e) => (
                        <span key={e} className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                          {e}
                        </span>
                      ))}
                      {h.failure_count > 0 && (
                        <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[11px] text-destructive">
                          {h.failure_count} failed {h.failure_count === 1 ? "attempt" : "attempts"} since the last success
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    {ts && (
                      <span
                        className={`text-[11px] ${
                          ts.state === "ok"
                            ? "text-emerald-500"
                            : ts.state === "err"
                            ? "text-destructive"
                            : "text-muted-foreground"
                        }`}
                      >
                        {ts.state === "sending" ? "sending…" : ts.state === "ok" ? `${ts.text} ✓` : `failed: ${ts.text}`}
                      </span>
                    )}
                    {h.active ? (
                      <button
                        type="button"
                        title="Send a test event to this endpoint"
                        disabled={ts?.state === "sending"}
                        onClick={() => sendTest(h.id)}
                        className="rounded p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
                      >
                        <Send className="h-4 w-4" />
                      </button>
                    ) : (
                      <button
                        type="button"
                        title="Switch it back on: same URL and signing secret"
                        onClick={() => enable(h.id)}
                        className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-foreground hover:bg-secondary"
                      >
                        <Power className="h-3.5 w-3.5" />
                        Enable
                      </button>
                    )}
                    <button
                      type="button"
                      title="Delete"
                      onClick={() => remove(h)}
                      className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {!h.active && (
                  <div className="mt-2 text-[11px] text-muted-foreground">
                    Switched off after a delivery spent its attempts with nothing
                    accepted since. Enable keeps its URL and signing secret; the
                    deliveries that fell due while it was off are not sent.
                  </div>
                )}
                {/* Only when we have it, which is only the moment it was
                    created. A listing that carried the signing secret would
                    hand anybody who can open this page the ability to forge a
                    delivery, so the server does not send it and this says so
                    instead of printing an empty box. */}
                {h.secret ? (
                  <div className="mt-2 flex items-center gap-2 rounded border border-primary/40 bg-primary/5 px-2 py-1.5">
                    <span className="text-[11px] font-medium text-foreground">
                      Signing secret: copy it now, this is the only time it is shown
                    </span>
                    <code className="truncate rounded bg-secondary px-2 py-0.5 font-mono text-[11px]">{h.secret}</code>
                    <CopyButton
                      text={h.secret}
                      label="Copy the signing secret"
                      className="text-muted-foreground hover:text-foreground"
                    />
                  </div>
                ) : (
                  <div className="mt-2 text-[11px] text-muted-foreground">
                    Signing secret shown once, when this endpoint was created.
                    Lost it? Delete this one and make another.
                    {h.last_error ? (
                      <>
                        {" "}Last failure: <code className="font-mono">{h.last_error}</code>
                      </>
                    ) : null}
                  </div>
                )}
                {h.legacy_signature ? (
                  <div className="mt-1.5 text-[11px] text-amber-500">
                    Not sent since 0.2.0: its secret is of before 0.1.11 (the
                    whole string is the HMAC key, which the Standard Webhooks
                    libraries cannot verify). Each delivery is refused, counted
                    among the failed attempts with the reason as the last
                    failure, and after three days of them the endpoint is
                    switched off. On the
                    server run{" "}
                    <code className="font-mono">manage rotate-webhook-secret</code>{" "}
                    and give your receiver the new secret: it keeps the id and
                    sends what is still queued (Enable it too if it was switched
                    off). Recreating this endpoint also works, without the queued
                    deliveries.
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      </Loaded>
    </div>
  );
}
