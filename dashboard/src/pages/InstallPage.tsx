import { useState, type ReactNode } from "react";
import { Plug, KeyRound, ArrowUpRight } from "lucide-react";
import { Link } from "react-router";
import { PageHeader } from "@/components/PageShell";
import { DOCS } from "@/lib/docsLinks";
import { cn } from "@/lib/cn";
import { useTarget, type Target } from "@/lib/target";
import { CopyButton } from "@/components/CopyButton";

// Every snippet here has to carry THIS install's address. The Python and Node
// tabs did not: they built a client with only a key, and a client with only a
// key talks to the hosted service at api.korely.ai. So the dashboard of a
// self-hosted box taught you to send your memories to somebody else's. The
// cURL and MCP tabs on this same page had the address right, which is what
// made it hard to see.
//
// Install/connect instructions. Mirrors the public docs at
// korely.ai/docs/surfaces — language-tabbed quickstart (Python / Node /
// cURL) with the same verified snippets, so a dev who lands here can copy-paste
// their way to a stored-and-recalled memory without leaving the dashboard.

type Lang = "python" | "node" | "curl";

interface Step {
  title: string;
  desc: ReactNode;
  fence: string; // code-fence label shown in the block header
  code: string;
}

const TABS: { id: Lang; label: string }[] = [
  { id: "python", label: "Python" },
  { id: "node", label: "Node.js" },
  { id: "curl", label: "cURL" },
];

const INSTALL = "Install the SDK";
const INIT = "Initialize the client";
const ADD = "Add a memory: the facts follow in seconds";
const RECALL = "Recall it, by meaning or as a prompt-ready block";

// Python and Node share the full 4-step path (install → init → add → recall).
// cURL skips install/init — just the two requests that matter. Built from the
// target (lib/target.ts): this install, or the hosted service.
function stepsFor(t: Target): Record<Lang, Step[]> {
  const key = `${t.prefix}...`;
  const where = t.sdkNeedsAddress ? (
    <>
      Point it at <span className="text-foreground">this install</span> and
      authenticate with your <code>{t.prefix}</code> key, or set{" "}
      <code>KORELY_BASE_URL</code> and <code>KORELY_API_KEY</code> and call
    </>
  ) : (
    <>
      Authenticate with your <code>{t.prefix}</code> key: the SDK finds the
      service from the key. Or set <code>KORELY_API_KEY</code> and call
    </>
  );
  // The two SDKs walk the same four steps in the same words: written once,
  // so a sentence corrected in one tab is corrected in the other (audit of
  // 17 Oct 2026).
  const sdkInstall = (runtime: string) =>
    `${runtime} or later. A thin HTTP client: no native build, no heavy dependencies.`;
  const sdkAdd = (
    <>
      One call stores the memory; a background worker mines the typed facts
      a few seconds later, and the reply says{" "}
      <code>processing</code> until they are there. A contradicting fact{" "}
      <span className="text-foreground">closes</span> the old one, which is
      never deleted.
    </>
  );
  const sdkRecall = (getContext: string) => (
    <>
      <code>search</code> returns the relevant memories;{" "}
      <code>{getContext}</code> assembles the active facts for one end user
      into a block you can drop straight into a prompt.
    </>
  );
  return {
  python: [
    {
      title: INSTALL,
      desc: sdkInstall("Python 3.9"),
      fence: "shell",
      code: `pip install korely-memory`,
    },
    {
      title: INIT,
      desc: (
        <>
          {where}{" "}
          <code>Korely()</code> with no arguments.
        </>
      ),
      fence: "python",
      code: `from korely_memory import Korely

korely = Korely(
${t.sdkNeedsAddress ? `    base_url="${t.base}",\n` : ""}    api_key="${key}",
)`,
    },
    {
      title: ADD,
      desc: sdkAdd,
      fence: "python",
      code: `korely.add(
    "Maria prefers email and is allergic to peanuts.",
    user_id="maria",
)`,
    },
    {
      title: RECALL,
      desc: sdkRecall("get_context"),
      fence: "python",
      code: `hits = korely.search("how do I reach her", user_id="maria")

ctx = korely.get_context(query="contact", user_id="maria")
print(ctx.context)
# → "## Known facts\\n- maria prefers email (since ...)"`,
    },
  ],
  node: [
    {
      title: INSTALL,
      desc: sdkInstall("Node 18"),
      fence: "shell",
      code: `npm install korely-memory`,
    },
    {
      title: INIT,
      desc: (
        <>
          {where}{" "}
          <code>new Korely()</code> with no arguments.
        </>
      ),
      fence: "typescript",
      code: `import { Korely } from "korely-memory";

const korely = new Korely({
${t.sdkNeedsAddress ? `  baseUrl: "${t.base}",\n` : ""}  apiKey: "${key}",
});`,
    },
    {
      title: ADD,
      desc: sdkAdd,
      fence: "typescript",
      code: `await korely.add(
  "Maria prefers email and is allergic to peanuts.",
  { user_id: "maria" },
);`,
    },
    {
      title: RECALL,
      desc: sdkRecall("getContext"),
      fence: "typescript",
      code: `const hits = await korely.search("how do I reach her", { user_id: "maria" });

const ctx = await korely.getContext({ query: "contact", user_id: "maria" });
console.log(ctx.context);
// → "## Known facts\\n- maria prefers email (since ...)"`,
    },
  ],
  curl: [
    {
      title: ADD,
      desc: "One POST stores the memory and queues it. A background worker mines the typed facts a few seconds later; ask for them with /v1/facts.",
      fence: "bash",
      code: `curl -X POST ${t.base}/v1/memories \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"content": "Maria prefers email and is allergic to peanuts.",
       "user_id": "maria"}'

# 201: stored, facts on their way
{ "id": "mem_8f2c…", "status": "processing", "facts": [] }

# a few seconds later
curl "${t.base}/v1/facts?user_id=maria" \\
  -H "Authorization: Bearer ${key}"`,
    },
    {
      title: RECALL,
      desc: (
        <>
          <code>/v1/memories/search</code> returns the relevant memories;{" "}
          <code>/v1/context</code> returns a prompt-ready block of the active
          facts for one end user.
        </>
      ),
      fence: "bash",
      code: `curl -X POST ${t.base}/v1/memories/search \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"query": "how do I reach her", "user_id": "maria"}'

# Or a prompt-ready block of the active facts:
curl "${t.base}/v1/context?query=contact&user_id=maria" \\
  -H "Authorization: Bearer ${key}"`,
    },
  ],
  };
}

export function InstallPage() {
  const [lang, setLang] = useState<Lang>("python");
  const target = useTarget();
  const STEPS = stepsFor(target);

  return (
    <div>
      <PageHeader
        title="Install"
        subtitle="Pick your language, grab a key, and store your first memory."
        docs={DOCS.install}
      />

      {/* Step 0 — point them at the key. The dashboard home mints the first
          key in one click, but Install can be reached directly. */}
      <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-primary/30 bg-primary/5 px-4 py-3 text-sm">
        <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <p className="text-muted-foreground">
          First, get a key.{" "}
          <Link
            to="/keys"
            className="font-medium text-foreground underline-offset-2 hover:underline"
          >
            Create one under API Keys
          </Link>
          : it looks like <code className="font-mono text-xs">{target.prefix}…</code>{" "}
          and authenticates every request below.
        </p>
      </div>

      {/* Language tabs — Python & Node share the full path; cURL is shorter. */}
      <div className="mb-6 inline-flex rounded-lg border border-border bg-secondary/40 p-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setLang(t.id)}
            className={cn(
              "rounded-md px-3.5 py-1.5 text-sm font-medium transition-colors",
              lang === t.id
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* The stepper. */}
      <div className="space-y-7">
        {STEPS[lang].map((s, i) => (
          <StepRow key={`${lang}-${i}`} n={i + 1} step={s} />
        ))}
      </div>

      {/* MCP — language-independent, so it lives below the tabs. */}
      <div className="mt-8 rounded-lg border border-border bg-card p-5">
        <div className="mb-1.5 flex items-center gap-2">
          <Plug className="h-4 w-4 text-muted-foreground" />
          <p className="text-sm font-medium">Prefer no code? Connect over MCP</p>
        </div>
        <p className="text-sm text-muted-foreground">
          Point any MCP-capable client (Claude, Cursor, Grok, ChatGPT) at the
          Korely connector. Your agent gets the same memory, no SDK required.
          Authenticate with the same <code>{target.prefix}</code> key you use for
          REST: without it the connector answers <code>401</code>, and the
          header is the one thing a client cannot guess.
        </p>
        <div className="mt-3">
          <CodeBlock
            fence="connector"
            code={`${target.mcp}

Authorization: Bearer ${target.prefix}...`}
          />
        </div>
      </div>

      {/* Escape hatch to the full reference. */}
      <div className="mt-6 flex flex-wrap gap-x-5 gap-y-2 text-sm">
        <DocLink {...DOCS.sdk} />
        <DocLink {...DOCS.cli} />
        <DocLink {...DOCS.api} />
      </div>
    </div>
  );
}

function StepRow({ n, step }: { n: number; step: Step }) {
  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)] md:gap-8">
      <div className="flex gap-3">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-foreground text-[11px] font-semibold text-background">
          {n}
        </span>
        <div>
          <p className="text-sm font-medium leading-snug">{step.title}</p>
          <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
            {step.desc}
          </p>
        </div>
      </div>
      <CodeBlock fence={step.fence} code={step.code} />
    </div>
  );
}

// The copy button says "Copied" only when the snippet was copied
// (components/CopyButton.tsx): over plain http on a LAN address there is no
// clipboard, and it showed its tick anyway (audit of 17 Oct 2026).
function CodeBlock({ fence, code }: { fence: string; code: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-secondary/50">
      <div className="flex items-center justify-between border-b border-border/70 px-3 py-1.5">
        <span className="font-mono text-[11px] tracking-wide text-muted-foreground">
          {fence}
        </span>
        <CopyButton
          text={code}
          label="Copy code"
          iconClassName="h-3 w-3"
          className="rounded-md px-1.5 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
        />
      </div>
      <pre className="overflow-x-auto px-4 py-3 font-mono text-[12.5px] leading-relaxed text-foreground">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function DocLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-muted-foreground underline-offset-2 transition-colors hover:text-foreground hover:underline"
    >
      {label}
      <ArrowUpRight className="h-3.5 w-3.5" />
    </a>
  );
}
