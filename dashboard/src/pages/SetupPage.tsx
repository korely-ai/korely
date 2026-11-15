import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { ArrowLeft, Check, ExternalLink, Loader2, Search } from "lucide-react";

import { FailureNote } from "@/components/LoadState";
import { NOT_REACHED, apiStatus, failure, type Failure } from "@/lib/apiError";
import {
  errorMessage,
  modelsApi,
  type ModelsCheck,
  type ModelsState,
  type ProviderOption,
  type Region,
} from "@/lib/agentsApi";
import { cn } from "@/lib/cn";
import { ProviderLogo } from "@/lib/providerLogos";
import { REGION_LABEL } from "@/components/RegionSelect";
import { SKIPPED } from "@/lib/setupSkipped";


// Two choices, each with the writer we chose and measured (2026-10-06): the
// other providers wait behind "Use another provider". The
// line under each names the server's recommended provider for that region
// (services/model_choice.PROVIDERS, its tagline), never a copy of it here:
// the copy is how the install script and this page came to suggest
// different models (audit of 17 Oct 2026).
// The regions and their names are the ones every other screen uses
// (components/RegionSelect.tsx), not a third copy (audit of 17 Oct 2026).
const REGIONS: Region[] = ["eu", "global"];

/**
 * The first thing after the first login (since 2026-10-02): where the
 * memories are processed, by which provider, with which key. The writing
 * model comes preselected from our measures. Reading is not here: the
 * customer's own model reads what Korely returns.
 *
 * Same canvas as the login page, so signing in and setting up read as one
 * flow; the dashboard starts when a writer is in force.
 */
export function SetupPage() {
  const navigate = useNavigate();
  const [state, setState] = useState<ModelsState | null>(null);
  const [step, setStep] = useState<1 | 2 | 3>(1);
  // Through "Use another provider": three steps instead of two.
  const [advanced, setAdvanced] = useState(false);
  const [region, setRegion] = useState<Region | null>(null);
  const [providerId, setProviderId] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [checked, setChecked] = useState<ModelsCheck | null>(null);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState<"check" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The read of the install's models, apart from the errors of a check or a
  // save: a 403 (an operator who is not the install's administrator, on an
  // install with several accounts) said "Could not reach this install. Is
  // the service running?" under cards that could do nothing (QA of 14 Nov
  // 2026). Said the screen's one way now (lib/apiError.ts, `failure`).
  const [readFailure, setReadFailure] = useState<Failure | null>(null);
  const [attempt, setAttempt] = useState(0);
  const refused = readFailure?.again === "never";

  useEffect(() => {
    modelsApi
      .get()
      .then((s) => {
        setReadFailure(null);
        setState(s);
        // The screen's own choice only: a writer of `.env` may be at an
        // address none of the providers here.
        if (s.active && s.source === "screen" && s.active.region) {
          setRegion(s.active.region);
          setProviderId(s.active.provider);
        }
      })
      .catch((e) =>
        setReadFailure(
          failure(
            e,
            apiStatus(e) === null ? NOT_REACHED : "The install could not say which model writes the memories.",
            Date.now(),
          ),
        ),
      );
  }, [attempt]);

  const provider = state?.providers.find((p) => p.id === providerId) ?? null;
  const inRegion = state?.providers.filter((p) => p.region === region) ?? [];
  const recommendedIn = (r: Region) =>
    state?.providers.find((p) => p.region === r && p.recommended) ?? null;

  const runCheck = async (typed?: string) => {
    if (!provider) return;
    setBusy("check");
    setError(null);
    try {
      const r = await modelsApi.check(provider.id, typed || undefined);
      setChecked(r);
      if (r.ok) {
        setModel(provider.model && r.models.includes(provider.model) ? provider.model : r.suggested ?? "");
      } else {
        setError(r.detail);
      }
    } catch (e) {
      setError(errorMessage(e) ?? `Could not reach ${provider.label}.`);
    } finally {
      setBusy(null);
    }
  };

  // A provider whose key is already saved lists its models at once.
  useEffect(() => {
    setChecked(null);
    setKey("");
    setError(null);
    if (step === 3 && provider?.key_set) runCheck();
    // On a new step or provider only: runCheck reads the state of now.
  }, [step, providerId]);

  const save = async () => {
    if (!provider || !model) return;
    setBusy("save");
    setError(null);
    try {
      await modelsApi.save(provider.id, { key: key.trim() || undefined, model });
      navigate("/", { replace: true });
    } catch (e) {
      setError(errorMessage(e) ?? "Could not save. Nothing was changed.");
      setBusy(null);
    }
  };

  return (
    <div className="min-h-screen bg-[#0B0F17] px-4 text-[#E2E8ED]">
      <div className="mx-auto flex max-w-[680px] flex-col pb-16 pt-[9vh]">
        <header className="mb-12 flex items-center justify-between">
          <span className="text-[15px] font-semibold tracking-tight">Korely</span>
          <Progress step={step === 3 && !advanced ? 2 : step} of={advanced ? 3 : 2} />
        </header>

        {step === 1 && !refused && (
          <section>
            <Eyebrow>Set up your memory</Eyebrow>
            <h1 className="mb-3 text-[28px] font-semibold leading-tight tracking-tight text-balance">
              Where should your memories be processed?
            </h1>
            <p className="mb-8 max-w-[52ch] text-[15px] leading-relaxed text-[#9CA3AF]">
              Every memory is read by a model that turns it into facts, next to the
              same person's last memories and facts. Choose where that happens. You
              can change it later in Settings.
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {REGIONS.map((r) => (
                <button
                  key={r}
                  type="button"
                  aria-pressed={region === r}
                  onClick={() => {
                    setRegion(r);
                    const ours = recommendedIn(r);
                    if (ours) {
                      setAdvanced(false);
                      setProviderId(ours.id);
                      setStep(3);
                    } else {
                      if (provider && provider.region !== r) setProviderId(null);
                      setAdvanced(true);
                      setStep(2);
                    }
                  }}
                  className={cn(
                    "group flex flex-col rounded-xl border p-5 text-left transition-colors",
                    region === r
                      ? "border-[#3B82F6]/70 bg-[#3B82F6]/6"
                      : "border-white/10 bg-white/2 hover:border-white/25",
                  )}
                >
                  <span className="mb-6 text-[11px] font-medium tracking-[0.16em] text-[#6B7280]">
                    {r.toUpperCase()}
                  </span>
                  <span className="mb-1.5 text-[17px] font-medium">{REGION_LABEL[r]}</span>
                  <span className="mb-5 text-[13px] leading-relaxed text-[#9CA3AF]">
                    {recommendedIn(r) ? `Written by ${recommendedIn(r)?.tagline}.` : "\u00a0"}
                  </span>
                  <span className="mt-auto flex items-center gap-3 text-[#6B7280] group-hover:text-[#9CA3AF]">
                    {(state?.providers ?? [])
                      .filter((p) => p.region === r)
                      .map((p) => (
                        <ProviderLogo key={p.id} id={p.id} className="h-4 w-4" />
                      ))}
                  </span>
                </button>
              ))}
            </div>
          </section>
        )}

        {step === 2 && region && (
          <section>
            <BackLink onClick={() => setStep(1)} />
            <Eyebrow>{REGION_LABEL[region]}</Eyebrow>
            <h1 className="mb-8 text-[28px] font-semibold leading-tight tracking-tight">
              Which provider do you have a key for?
            </h1>
            <div
              className={cn(
                "grid gap-3",
                inRegion.length > 2 ? "grid-cols-1 sm:grid-cols-3" : "grid-cols-1 sm:grid-cols-2",
              )}
            >
              {inRegion.map((p, i) => (
                <ProviderTile
                  key={p.id}
                  p={p}
                  // Five in three columns: the last one closes the row.
                  wide={inRegion.length === 5 && i === 4}
                  selected={providerId === p.id}
                  onPick={() => {
                    setProviderId(p.id);
                    setAdvanced(true);
                    setStep(3);
                  }}
                />
              ))}
            </div>
          </section>
        )}

        {step === 3 && provider && (
          <section>
            <BackLink onClick={() => setStep(advanced ? 2 : 1)} />
            <div className="mb-2 flex items-center gap-2.5 text-[#9CA3AF]">
              <ProviderLogo id={provider.id} className="h-5 w-5 text-[#E2E8ED]" />
              <span className="text-[13px]">{provider.label}</span>
            </div>
            <h1 className="mb-8 text-[28px] font-semibold leading-tight tracking-tight">
              Connect {provider.label}
            </h1>

            <div className="mb-2 flex items-baseline justify-between">
              <label htmlFor="key" className="text-[13px] text-[#9CA3AF]">
                API key
              </label>
              <a
                href={provider.key_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[13px] text-[#9CA3AF] hover:text-[#E2E8ED]"
              >
                Get a key <ExternalLink className="h-3 w-3" />
              </a>
            </div>
            <div className="flex gap-2">
              <input
                id="key"
                type="password"
                value={key}
                onChange={(e) => {
                  setKey(e.target.value);
                  setChecked(null);
                }}
                placeholder={
                  provider.key_set ? `Saved key ending in ${provider.last_four}` : provider.key_hint
                }
                autoComplete="off"
                spellCheck={false}
                className="min-w-0 flex-1 rounded-lg border border-white/10 bg-white/4 px-3 py-2.5 font-mono text-[14px] outline-hidden placeholder:font-sans placeholder:text-[#6B7280] focus:border-[#3B82F6]/60"
              />
              <button
                type="button"
                onClick={() => runCheck(key.trim())}
                disabled={busy !== null || (!key.trim() && !provider.key_set)}
                className="rounded-lg border border-white/15 px-4 text-[14px] font-medium transition-colors hover:border-white/30 disabled:opacity-40"
              >
                {busy === "check" ? <Loader2 className="h-4 w-4 animate-spin" /> : "Check"}
              </button>
            </div>
            <p className="mt-2 min-h-[20px] text-[13px]">
              {checked?.ok ? (
                <span className="inline-flex items-center gap-1.5 text-[#34D399]">
                  <Check className="h-3.5 w-3.5" />
                  The key works. {checked.models.length} models available.
                </span>
              ) : (
                <span className="text-[#6B7280]">
                  Checked with {provider.label} before it is stored, and stored encrypted.
                </span>
              )}
            </p>

            {checked?.ok && (
              <ModelPicker provider={provider} checked={checked} model={model} onPick={setModel} />
            )}

            {error && (
              <p role="alert" className="mt-5 text-[13px] leading-relaxed text-[#F87171]">
                {error}
              </p>
            )}

            {checked?.ok && (
              <button
                type="button"
                onClick={save}
                disabled={busy !== null || !model || state?.can_store === false}
                className="mt-8 w-full rounded-lg bg-[#3B82F6] py-3 text-[15px] font-medium text-white transition-colors hover:bg-[#2563EB] disabled:opacity-40 disabled:hover:bg-[#3B82F6]"
              >
                {busy === "save" ? "Saving…" : "Save and open the dashboard"}
              </button>
            )}
            {checked?.ok && state && !state.embeddings.ready && provider.id !== "google" && (
              <p className="mt-4 text-[13px] leading-relaxed text-[#FBBF24]">
                Search by meaning will be off: this install computes its vectors with
                Gemini and has no Google key. Set EMBEDDING_PROVIDER=local in .env and run
                docker compose up -d --build to compute them on this machine instead.
              </p>
            )}
            {state?.can_store === false && (
              <p className="mt-3 text-[13px] text-[#FBBF24]">
                This install has no SECRET_KEY, so there is nowhere safe to keep a key. Put
                a long random string in .env and restart.
              </p>
            )}
            {provider.recommended && (
              <button
                type="button"
                onClick={() => {
                  setAdvanced(true);
                  setStep(2);
                }}
                className="mt-8 self-start text-left text-[13px] text-[#6B7280] underline-offset-2 hover:text-[#9CA3AF] hover:underline"
              >
                Use another provider
              </button>
            )}
          </section>
        )}

        {step === 1 && refused && readFailure && (
          <FailureNote failure={readFailure} />
        )}
        {step === 1 && refused && (
          <button
            type="button"
            onClick={() => navigate("/", { replace: true })}
            className="mt-6 self-start text-left text-[13px] text-[#6B7280] underline-offset-2 hover:text-[#9CA3AF] hover:underline"
          >
            Go to the dashboard
          </button>
        )}

        {step === 1 && !refused && (
          <button
            type="button"
            onClick={() => {
              try {
                localStorage.setItem(SKIPPED, "1");
              } catch {
                // No storage: the screen asks again next time, nothing worse.
              }
              navigate("/", { replace: true });
            }}
            className="mt-10 self-start text-left text-[13px] text-[#6B7280] underline-offset-2 hover:text-[#9CA3AF] hover:underline"
          >
            Skip for now: run without a model. Memories are stored and wait for one;
            until then facts come only from what you write with POST /v1/facts.
          </button>
        )}

        {step === 1 && error && (
          <p role="alert" className="mt-6 text-[13px] text-[#F87171]">
            {error}
          </p>
        )}

        {step === 1 && readFailure && !refused && (
          <FailureNote failure={readFailure} onRetry={() => setAttempt((n) => n + 1)} className="mt-6" />
        )}
      </div>
    </div>
  );
}

function Progress({ step, of }: { step: 1 | 2 | 3; of: 2 | 3 }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-[12px] tabular-nums text-[#6B7280]">
        Step {step} of {of}
      </span>
      <div className="flex gap-1.5" aria-hidden="true">
        {Array.from({ length: of }, (_, i) => i + 1).map((n) => (
          <span
            key={n}
            className={cn("h-1 w-6 rounded-full", n <= step ? "bg-[#3B82F6]" : "bg-white/10")}
          />
        ))}
      </div>
    </div>
  );
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-3 text-[11px] font-medium uppercase tracking-[0.14em] text-[#6B7280]">
      {children}
    </p>
  );
}

function BackLink({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mb-8 inline-flex items-center gap-1.5 text-[13px] text-[#9CA3AF] hover:text-[#E2E8ED]"
    >
      <ArrowLeft className="h-3.5 w-3.5" /> Back
    </button>
  );
}

function ProviderTile({
  p,
  wide,
  selected,
  onPick,
}: {
  p: ProviderOption;
  wide?: boolean;
  selected: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onPick}
      className={cn(
        "flex flex-col items-start rounded-xl border p-4 text-left transition-colors",
        wide && "sm:col-span-2",
        selected
          ? "border-[#3B82F6]/70 bg-[#3B82F6]/6"
          : "border-white/10 bg-white/2 hover:border-white/25",
      )}
    >
      <ProviderLogo id={p.id} className="mb-4 h-6 w-6" />
      <span className="text-[15px] font-medium">{p.label}</span>
      <span className="mt-0.5 text-[13px] text-[#9CA3AF]">{p.tagline}</span>
      {p.key_set && (
        <span className="mt-3 text-[12px] text-[#6B7280]">Key saved, ending in {p.last_four}</span>
      )}
    </button>
  );
}

function ModelPicker({
  provider,
  checked,
  model,
  onPick,
}: {
  provider: ProviderOption;
  checked: ModelsCheck;
  model: string;
  onPick: (m: string) => void;
}) {
  const [query, setQuery] = useState("");
  const suggested = checked.suggested;
  const q = query.trim().toLowerCase();

  const rows = useMemo(() => {
    if (q) return checked.models.filter((m) => m.toLowerCase().includes(q)).slice(0, 60);
    const head = suggested ? [suggested] : [];
    const rest = (checked.top.length ? checked.top : checked.models).filter(
      (m) => m !== suggested && m !== suggested?.split(":")[0],
    );
    return [...head, ...rest];
  }, [q, checked, suggested]);

  const note =
    provider.measured[model] ??
    (model === suggested
      ? "Fast and inexpensive. Not measured by Korely yet."
      : "Your choice. We have not measured it.");

  return (
    <div className="mt-8">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-[15px] font-medium">The model that writes your memories</h2>
        <span className="text-[12px] text-[#6B7280]">
          {q ? "Search results" : checked.top.length ? "Most used this week" : "All models"}
        </span>
      </div>
      <div className="relative mb-2">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[#6B7280]" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`Search ${checked.models.length} models`}
          className="w-full rounded-lg border border-white/10 bg-white/4 py-2 pl-8 pr-3 text-[14px] outline-hidden placeholder:text-[#6B7280] focus:border-[#3B82F6]/60"
        />
      </div>
      <ul
        role="radiogroup"
        aria-label="Writing model"
        className="max-h-72 overflow-y-auto rounded-lg border border-white/10"
      >
        {rows.map((m) => (
          <li key={m} className="border-b border-white/6 last:border-b-0">
            <button
              type="button"
              role="radio"
              aria-checked={model === m}
              onClick={() => onPick(m)}
              className={cn(
                "flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors",
                model === m ? "bg-[#3B82F6]/8" : "hover:bg-white/3",
              )}
            >
              <span
                className={cn(
                  "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
                  model === m ? "border-[#3B82F6] bg-[#3B82F6]" : "border-white/25",
                )}
              >
                {model === m && <span className="h-1.5 w-1.5 rounded-full bg-white" />}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono text-[13px]">{m}</span>
              {m === suggested && (
                <span className="shrink-0 rounded-full bg-[#3B82F6]/15 px-2 py-0.5 text-[11px] font-medium text-[#93C5FD]">
                  Recommended
                </span>
              )}
            </button>
          </li>
        ))}
        {rows.length === 0 && (
          <li className="px-3 py-6 text-center text-[13px] text-[#6B7280]">No model matches.</li>
        )}
      </ul>
      <p className="mt-2.5 text-[13px] leading-relaxed text-[#9CA3AF]">{note}</p>
    </div>
  );
}
