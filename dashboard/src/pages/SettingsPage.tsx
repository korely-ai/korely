import { Fragment, useState } from "react";
import { Link } from "react-router";
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  KeyRound,
  Mail,
  MessageSquare,
  ScrollText,
  Server,
  ShieldCheck,
} from "lucide-react";

import { PageHeader, fieldClass } from "@/components/PageShell";
import { useAuth } from "@/contexts/AuthContext";
import { useMode } from "@/contexts/ModeContext";
import {
  errorCode,
  errorMessage,
  modelsApi,
  providerApi,
  regionsLeftWithoutWriter,
  type ModelsState,
  type ProviderState,
} from "@/lib/agentsApi";
import { ProviderLogo } from "@/lib/providerLogos";
import { FEEDBACK_EMAIL, SALES_EMAIL, SITE, SUPPORT_EMAIL } from "@/lib/docsLinks";
import { REGION_LABEL } from "@/components/RegionSelect";
import { useLoad } from "@/lib/useLoad";
import { ErrorNote, Loaded } from "@/components/LoadState";

/**
 * The model and its key are the service's on the Cloud: the server refuses
 * an account their reads with `set_by_the_service` (api/admin.py,
 * _install_wide), and there is nothing of the account's to show. That
 * refusal is the one answer a section hides on; every other failure is said
 * (lib/useLoad.ts). Both reads used to end in `.catch(() => setState(null))`,
 * so a failed read hid the writing model, and the key read "not set"
 * (audit of 17 Oct 2026).
 */
const SERVICES = "set_by_the_service";
const CONFIRM = "confirmation_required";

function unlessTheServices<T>(read: Promise<T>): Promise<T | typeof SERVICES> {
  return read.catch((e) => {
    if (errorCode(e) === SERVICES) return SERVICES;
    throw e;
  });
}

/**
 * Whether a section whose read `unlessTheServices` makes is drawn, from what
 * the read holds (`data` of lib/useLoad.ts: null while it loads and after it
 * fails). On an install of one's own the sections are the person's: drawn
 * while loading and on a failure, which they say. On the Cloud they are the
 * superadmin's, and only an answer proves the viewer is the superadmin: a
 * read that failed (a 5xx, the network) drew the writing model "billed to
 * your key" and a field for a Google key to a customer. Safe first: nothing
 * of them on the Cloud until the server has answered; the superadmin sees
 * them again when a read answers.
 */
export function drawnHere<T>(hosted: boolean, data: T | typeof SERVICES | null): boolean {
  if (data === SERVICES) return false;
  return !hosted || data !== null;
}

/**
 * Which model writes the memories: the choice made at first login (/setup),
 * or what .env says. Reading is the customer's own model, so it is not here.
 */
export function WritingModel() {
  const { hosted } = useMode();
  const models = useLoad(
    () => unlessTheServices(modelsApi.get()),
    [],
    "The writing model could not be read.",
  );
  // "Use what .env says" awaited the clear with nothing around it: a refusal
  // or a dropped connection left the screen's model writing, and the page
  // said nothing (audit of 17 Oct 2026).
  const [envError, setEnvError] = useState<string | null>(null);

  // On the Cloud the answer is nearly always the service's: nothing is drawn
  // until it comes, nor when it fails (`drawnHere`).
  if (!drawnHere(hosted, models.data)) return null;

  const backToEnv = async () => {
    setEnvError(null);
    const unchanged = "Nothing changed: the model chosen here still writes. Try again.";
    try {
      await modelsApi.clear();
    } catch (e) {
      // The Cloud refuses a clear that would leave a region with no writer
      // and names each one (api/admin.clear_models): on 5 Nov 2026 this
      // button left the projects in Global with no provider, in silence.
      // Asked as the other clicks that stop something are (API keys).
      if (errorCode(e) !== CONFIRM) {
        setEnvError(errorMessage(e) ?? unchanged);
        return;
      }
      // The page's own sentence, from the regions the server named: its
      // message is for a program and names `?confirm=true`.
      const lost = regionsLeftWithoutWriter(e);
      const whose = lost.length
        ? lost.map((r) => `Projects in ${REGION_LABEL[r]}`).join(" and ")
        : "Some projects";
      if (
        !confirm(
          `${whose} would be left with no provider: their memories would wait, with no ` +
            "facts extracted, until a provider is saved again. On the Cloud .env sets no " +
            "writer. Forget the writers anyway?",
        )
      )
        return;
      try {
        await modelsApi.clear(true);
      } catch (again) {
        setEnvError(errorMessage(again) ?? unchanged);
        return;
      }
    }
    models.reload();
  };

  return (
    <section className="mb-6 rounded-lg border border-border bg-card p-5">
      <div className="mb-1 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Cpu className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-medium">Writing model</h2>
        </div>
        <Link
          to="/setup"
          className="rounded-md border border-input px-3 py-1.5 text-sm transition-colors hover:bg-muted"
        >
          Change
        </Link>
      </div>
      <p className="mb-4 text-[13px] text-muted-foreground">
        The model that turns each memory into facts, billed to your key. Your
        agent's own model does the reading.
      </p>
      <Loaded load={models}>
        {(state) =>
          state === SERVICES ? null : (
            <WritingModelState state={state} hosted={hosted} onBackToEnv={backToEnv} />
          )
        }
      </Loaded>
      {envError && <ErrorNote className="mt-3">{envError}</ErrorNote>}
    </section>
  );
}

const REGIONS = ["eu", "global"] as const;

function WritingModelState({
  state,
  hosted,
  onBackToEnv,
}: {
  state: ModelsState;
  hosted: boolean;
  onBackToEnv: () => void;
}) {
  const a = state.active;
  // On the Cloud every region is offered to every project, so one with no
  // writer is a fault, said in red (5 Nov 2026: Global had none for six
  // minutes and the page said it in grey). On an install of one's own a
  // region nobody set up is only unused.
  const without = hosted ? REGIONS.filter((r) => !state.by_region?.[r]) : [];
  return (
    <>
      {without.length > 0 && (
        <ErrorNote className="mb-4">
          {without.map((r) => `Projects in ${REGION_LABEL[r]}`).join(" and ")} have no
          provider: their memories are stored and wait, and no facts are extracted until one
          is saved (Change).
        </ErrorNote>
      )}
      <dl className="grid grid-cols-[140px_1fr] gap-y-2 wrap-anywhere text-[13px]">
        {/* The Cloud's default region is the service's, never the provider
            saved last (core/llm.default_region). */}
        {hosted && state.default_region && (
          <>
            <dt className="text-muted-foreground">Default region</dt>
            <dd>
              {REGION_LABEL[state.default_region]}
              <span className="block text-muted-foreground">
                The service's: a project that never chose a region writes here. Saving a
                provider makes it the writer of its own region only.
              </span>
            </dd>
          </>
        )}
        <dt className="text-muted-foreground">{hosted ? "Saved last in" : "Region"}</dt>
        <dd>{a ? (a.region ? REGION_LABEL[a.region] : "None of the known providers'") : "—"}</dd>
        <dt className="text-muted-foreground">Provider</dt>
        <dd className="flex items-center gap-2">
          {/* A writer of .env at an address none of the known providers'
              (`region` null) is nobody's brand: it wore Mistral's mark,
              the vendor its calls are counted under (review of 28 Oct 2026). */}
          {a &&
            (a.region ? (
              <ProviderLogo id={a.provider} className="h-3.5 w-3.5" />
            ) : (
              <Server className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
            ))}
          {a ? a.label : "Not set"}
          {state.source === "env" && (
            <span className="text-muted-foreground">(set in .env)</span>
          )}
        </dd>
        <dt className="text-muted-foreground">Model</dt>
        <dd className="font-mono text-xs">{a?.model ?? "—"}</dd>
        <dt className="text-muted-foreground">Key</dt>
        <dd>{a ? `ending in ${a.last_four}` : "—"}</dd>
        <dt className="text-muted-foreground">Vectors</dt>
        <dd>
          {state.embeddings.local
            ? "Computed on this machine"
            : state.embeddings.ready
              ? `${state.embeddings.provider} (${state.embeddings.model})`
              : "Off: Gemini vectors need a Google key, or set EMBEDDING_PROVIDER=local"}
        </dd>
      </dl>
      {/* A project may choose its region (Projects page): it then writes with
          the provider saved for that region, and waits when there is none. */}
      {/* On an install of one's own the regions show once the Models page
          chose a writer, as before; their rows say the file's writer too. */}
      {(hosted || REGIONS.some((r) => state.by_region?.[r]?.source === "screen")) && (
        <dl className="mt-4 grid grid-cols-[140px_1fr] gap-y-2 wrap-anywhere border-t border-border pt-4 text-[13px]">
          {REGIONS.map((r) => {
            const w = state.by_region?.[r];
            return (
              <Fragment key={r}>
                <dt className="text-muted-foreground">Projects in {REGION_LABEL[r]}</dt>
                <dd>
                  {w ? (
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                      <ProviderLogo id={w.provider} className="h-3.5 w-3.5 shrink-0" />
                      <span className="whitespace-nowrap">{w.label}</span>
                      <span className="font-mono text-xs">{w.model}</span>
                      {w.source === "env" && (
                        <span className="whitespace-nowrap text-muted-foreground">
                          (set in .env)
                        </span>
                      )}
                    </span>
                  ) : hosted ? (
                    <span className="flex items-center gap-1.5 font-medium text-destructive">
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                      No provider: their memories wait
                    </span>
                  ) : (
                    <span className="text-muted-foreground">No provider: their memories wait</span>
                  )}
                </dd>
              </Fragment>
            );
          })}
        </dl>
      )}
      {state.source === "screen" && (
        <button
          onClick={onBackToEnv}
          className="mt-4 text-[13px] text-muted-foreground underline underline-offset-2 hover:text-foreground"
        >
          Use what .env says instead
        </button>
      )}
    </>
  );
}

/**
 * What the saved key does, as the server says it (GET and PUT
 * /admin/provider, `uses`). The page said "Extraction and embeddings now go
 * through this key" after every save, while the help above it says the key
 * writes only when no model is chosen and makes the vectors only with Gemini
 * (audit of 17 Oct 2026).
 */
function whatTheKeyDoes(uses: ProviderState["uses"] | undefined): string {
  const writes = `it writes the memories of the projects in ${REGION_LABEL.global}`;
  const vectors = "it computes the vectors";
  if (uses?.writes && uses.vectors) return `Saved and in use: ${writes}, and ${vectors}.`;
  if (uses?.writes) return `Saved and in use: ${writes}. The vectors are computed on this machine.`;
  if (uses?.vectors) return `Saved and in use: ${vectors}. The writing model above writes the memories.`;
  return "Saved. Nothing uses it now: the writing model above writes the memories, and the vectors are computed on this machine.";
}

/**
 * Where the model key goes.
 *
 * Extraction and embeddings are the only things here that call a model, and
 * on an install you own that call is billed to your own provider account. So
 * the key is yours, you paste it here, and it is checked against the provider
 * before it is stored: a key that is saved and does not work is the same
 * silent failure as everything else, except you find out hours later when a
 * write quietly mines nothing.
 */
export function ModelProvider() {
  const provider = useLoad(
    () => unlessTheServices(providerApi.get()),
    [],
    "The Google key's state could not be read.",
  );
  const state: ProviderState | null = provider.data === SERVICES ? null : provider.data;
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null);

  const save = async () => {
    setBusy("save");
    setSaid(null);
    try {
      const saved = await providerApi.save(key.trim());
      provider.reload();
      setKey("");
      setSaid({ ok: true, text: whatTheKeyDoes(saved.uses) });
    } catch (e) {
      setSaid({ ok: false, text: errorMessage(e) ?? "Could not save the key." });
    } finally {
      setBusy(null);
    }
  };

  const test = async () => {
    setBusy("test");
    setSaid(null);
    const typed = key.trim();
    try {
      const r = await providerApi.test(typed || undefined);
      // A green tick after Test used to say only "the provider answered",
      // which reads as "done". It is not: nothing has been stored. Somebody
      // pressed Test, saw green, closed the page, and the install carried on
      // with the old key. The message now says which key was tried and what
      // still has to happen.
      setSaid({
        ok: r.ok,
        text: r.ok
          ? typed
            ? "This key works. It is NOT saved yet: press Save."
            : "The key already in use works. Nothing changed."
          : r.detail,
      });
    } catch (e) {
      setSaid({ ok: false, text: errorMessage(e) ?? "Could not reach the provider." });
    } finally {
      setBusy(null);
    }
  };

  // On the hosted service an account's read is refused (the key is the
  // service's): nothing to show. The superadmin's read answers, and only
  // an answer draws it there (`drawnHere`).
  const { hosted } = useMode();
  if (!drawnHere(hosted, provider.data)) return null;

  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-5">
      <div className="mb-1 flex items-center gap-2">
        <KeyRound className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-sm font-medium">Google key (Gemini)</h2>
      </div>
      <p className="mb-4 text-[13px] text-muted-foreground">
        Used for writing when no model is chosen above, and for vectors when
        the install computes them with Gemini. Paste your Google AI Studio
        key; it is checked before it is stored, and stored encrypted.
      </p>

      {/* What is set is said only from an answer: a failed read is not
          "not set". */}
      <Loaded load={provider} className="mb-4">
        {(read) =>
          read === SERVICES ? null : (
            <dl className="mb-4 grid grid-cols-[140px_1fr] gap-y-2 wrap-anywhere text-[13px]">
              <dt className="text-muted-foreground">Key</dt>
              <dd>
                {read.set
                  ? `set, ending ${read.last_four}${read.source === "env" ? " (from the install's .env)" : ""}`
                  : "not set"}
              </dd>
              <dt className="text-muted-foreground">Last model call</dt>
              <dd>
                {read.last_call?.state === "working"
                  ? `working, ${read.last_call.seconds_ago}s ago`
                  : read.last_call?.state === "failing"
                    ? `failing, ${read.last_call.seconds_ago}s ago: ${read.last_call.detail}`
                    : "nothing has called it yet"}
              </dd>
              {read.updated_by ? (
                <>
                  <dt className="text-muted-foreground">Set by</dt>
                  <dd>{read.updated_by}</dd>
                </>
              ) : null}
            </dl>
          )
        }
      </Loaded>

      {state && !state.can_store && (
        <p className="mb-3 flex items-start gap-2 text-[13px] text-amber-700 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            This install has no <code>SECRET_KEY</code>, so there is nowhere
            safe to keep a key and saving will refuse rather than write it in
            clear text. Put a long random string in <code>.env</code> and
            restart.
          </span>
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="AIza..."
          autoComplete="off"
          className={`min-w-0 basis-full sm:basis-0 sm:flex-1 ${fieldClass}`}
        />
        <button
          onClick={test}
          disabled={busy !== null || (!key.trim() && !state?.set)}
          className="whitespace-nowrap rounded-md border border-input px-3 py-2 text-sm disabled:opacity-40"
        >
          {busy === "test" ? "Testing…" : "Test only"}
        </button>
        <button
          onClick={save}
          disabled={busy !== null || key.trim().length < 8}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
        >
          {busy === "save" ? "Checking and saving…" : "Save"}
        </button>
      </div>

      {said && (
        <p
          role="status"
          className={`mt-3 flex items-start gap-2 text-[13px] ${said.ok ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}`}
        >
          {said.ok ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <span>{said.text}</span>
        </p>
      )}
    </div>
  );
}

/** "Sign out of all devices", as in the hosted product: two steps, because
 *  the second one signs this tab out too. */
function SignOutEverywhere() {
  const { logoutEverywhere } = useAuth();
  const [asking, setAsking] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  const go = async () => {
    setFailed(null);
    try {
      await logoutEverywhere();
    } catch (e) {
      setFailed(errorMessage(e) ?? "Could not reach this install: nothing was signed out.");
    }
  };

  return (
    <div className="mt-4 border-t border-border pt-4 text-sm">
      {!asking ? (
        <button
          onClick={() => setAsking(true)}
          className="rounded-md border border-input px-3 py-1.5 text-sm transition-colors hover:bg-muted"
        >
          Sign out of all devices
        </button>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] text-muted-foreground">
            Every browser signed in as you stops working, this one included.
          </span>
          <button
            onClick={go}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
          >
            Sign out everywhere
          </button>
          <button
            onClick={() => setAsking(false)}
            className="rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      )}
      {failed && <p className="mt-2 text-[13px] text-amber-700 dark:text-amber-400">{failed}</p>}
    </div>
  );
}

export function SettingsPage() {
  const { user } = useAuth();
  // On the hosted service the model and its key are the service's: the
  // server refuses those routes to an account (403), and both sections hide
  // themselves until a read answers (`drawnHere`). The superadmin, whom the
  // server lets in, sees and sets them here.
  const { hosted } = useMode();
  return (
    <div>
      <PageHeader
        title="Settings"
        subtitle={hosted ? "Your sign-in and sessions." : "This install, and your account."}
      />
      <WritingModel />
      <ModelProvider />
      <div className="rounded-lg border border-border bg-card p-5">
        <dl className="grid grid-cols-[120px_1fr] gap-y-3 text-sm">
          {/* An install signs in with a username, the Cloud with an email. */}
          <dt className="text-muted-foreground">{hosted ? "Email" : "Username"}</dt>
          <dd>{user?.name ?? "—"}</dd>
          <dt className="text-muted-foreground">Sign-in</dt>
          <dd className="capitalize">
            {user?.signIn?.replace(".com", "")?.replace("oidc.", "") ?? "—"}
          </dd>
          <dt className="text-muted-foreground">User ID</dt>
          <dd className="font-mono text-xs text-muted-foreground">
            {user?.uid ?? "—"}
          </dd>
        </dl>
        <SignOutEverywhere />
      </div>

      {/* Support & resources — a human to reach, and where to read more. */}
      <h2 className="mb-2 mt-8 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        Help &amp; support
      </h2>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {/* The Cloud has nothing to install: its customers write to the
            address its own refusals already name, not to the
            self-hosted product's commercial support (audit of 17 Oct 2026). */}
        {hosted ? (
          <SupportLink
            icon={<Mail className="h-4 w-4" />}
            title="Support"
            desc={`Questions about your account, your plan or a bill: ${SUPPORT_EMAIL}.`}
            href={`mailto:${SUPPORT_EMAIL}`}
          />
        ) : (
          <SupportLink
            icon={<Mail className="h-4 w-4" />}
            title="Support"
            desc={`Installation help and a reply within one working day come with commercial support, priced on request: ${SALES_EMAIL}.`}
            href={`${SITE}/pricing`}
          />
        )}
        <SupportLink
          icon={<MessageSquare className="h-4 w-4" />}
          title="Send feedback"
          desc="Tell us what your agent needs next."
          href={`mailto:${FEEDBACK_EMAIL}?subject=${encodeURIComponent(
            "Korely Agents feedback",
          )}&body=${encodeURIComponent(
            `From: ${user?.name ?? ""}\nUser ID: ${user?.uid ?? ""}\n\n`,
          )}`}
        />
        <SupportLink
          icon={<ScrollText className="h-4 w-4" />}
          title="Changelog"
          desc="What shipped, newest first."
          href={`${SITE}/changelog`}
          external
        />
        <SupportLink
          icon={<ShieldCheck className="h-4 w-4" />}
          title="Security & data residency"
          desc="Sub-processors, encryption, erasure."
          href={`${SITE}/security`}
          external
        />
      </div>
    </div>
  );
}

function SupportLink({
  icon,
  title,
  desc,
  href,
  external,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  href: string;
  external?: boolean;
}) {
  return (
    <a
      href={href}
      {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
      className="flex items-start gap-3 rounded-lg border border-border bg-card p-4 transition-colors hover:border-foreground/20"
    >
      <span className="mt-0.5 text-muted-foreground">{icon}</span>
      <span>
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-xs text-muted-foreground">{desc}</span>
      </span>
    </a>
  );
}
