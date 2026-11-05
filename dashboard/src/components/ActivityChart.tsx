import { useId, useMemo, useState } from "react";
import type { DailyBucket } from "@/lib/agentsApi";

// A dependency-free SVG line/area chart for the daily activity series. One
// "Total" line by default, or a writes/reads breakdown when toggled — the
// Mem0 dashboard pattern. Plots whatever the backend returns; no synthetic
// data, no smoothing that invents points.

type Series = { key: "total" | "writes" | "reads"; label: string; color: string };

const TOTAL: Series[] = [{ key: "total", label: "Total requests", color: "hsl(var(--primary))" }];
const BREAKDOWN: Series[] = [
  { key: "writes", label: "Writes", color: "hsl(var(--primary))" },
  { key: "reads", label: "Reads", color: "hsl(217 91% 72%)" },
];

const W = 760;
const H = 220;
const PAD = { top: 16, right: 12, bottom: 28, left: 40 };

function valueOf(b: DailyBucket, key: Series["key"]): number {
  if (key === "writes") return b.writes;
  if (key === "reads") return b.reads;
  return b.writes + b.reads;
}

function fmtDate(iso: string): string {
  // "2026-06-17" → "17 Jun"
  const [, m, d] = iso.split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(d)} ${months[Number(m) - 1] ?? ""}`;
}

function fmtNum(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

export function ActivityChart({ buckets }: { buckets: DailyBucket[] }) {
  const [breakdown, setBreakdown] = useState(false);
  const series = breakdown ? BREAKDOWN : TOTAL;
  const gradId = useId();

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;

  const max = useMemo(() => {
    let m = 0;
    for (const b of buckets) {
      m = Math.max(m, breakdown ? Math.max(b.writes, b.reads) : b.writes + b.reads);
    }
    return m;
  }, [buckets, breakdown]);

  // Nice round ceiling so the top gridline reads cleanly.
  const ceil = niceCeil(max);
  const n = buckets.length;
  const x = (i: number) => PAD.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (ceil === 0 ? 0 : (v / ceil) * innerH);

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(ceil * f));
  // Show at most ~6 x-labels so they don't collide.
  const labelEvery = Math.max(1, Math.ceil(n / 6));

  const total = buckets.reduce((s, b) => s + b.writes + b.reads, 0);

  return (
    <div>
      <div className="mb-1 flex items-baseline gap-3">
        <p className="text-2xl font-semibold tracking-tight">{total.toLocaleString()}</p>
        <span className="text-xs text-muted-foreground">requests in range</span>
      </div>

      {total === 0 ? (
        <div className="flex h-[220px] items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
          No requests in this range yet.
        </div>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Daily request activity">
          {/* gridlines + y labels */}
          {ticks.map((t, idx) => {
            const yy = y(t);
            return (
              <g key={idx}>
                <line x1={PAD.left} x2={W - PAD.right} y1={yy} y2={yy} stroke="hsl(var(--border))" strokeWidth={1} />
                <text x={PAD.left - 6} y={yy + 3} textAnchor="end" className="fill-muted-foreground" style={{ fontSize: 10 }}>
                  {fmtNum(t)}
                </text>
              </g>
            );
          })}

          {/* x labels */}
          {buckets.map((b, i) =>
            i % labelEvery === 0 || i === n - 1 ? (
              <text key={b.date} x={x(i)} y={H - 8} textAnchor="middle" className="fill-muted-foreground" style={{ fontSize: 10 }}>
                {fmtDate(b.date)}
              </text>
            ) : null,
          )}

          {series.map((s) => {
            const pts = buckets.map((b, i) => [x(i), y(valueOf(b, s.key))] as const);
            const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");
            const area = `${line} L${x(n - 1)},${PAD.top + innerH} L${x(0)},${PAD.top + innerH} Z`;
            const isPrimary = s.color === "hsl(var(--primary))";
            return (
              <g key={s.key}>
                {isPrimary && (
                  <>
                    <defs>
                      <linearGradient id={`${gradId}-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={s.color} stopOpacity={0.18} />
                        <stop offset="100%" stopColor={s.color} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <path d={area} fill={`url(#${gradId}-${s.key})`} />
                  </>
                )}
                <path d={line} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              </g>
            );
          })}
        </svg>
      )}

      {/* legend + breakdown toggle */}
      <div className="mt-2 flex items-center justify-between">
        <div className="flex items-center gap-3">
          {series.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setBreakdown((v) => !v)}
          className="inline-flex items-center gap-2 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          Breakdown
          <span
            className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors ${breakdown ? "bg-primary" : "bg-secondary"}`}
          >
            <span
              className={`inline-block h-3 w-3 rounded-full bg-background transition-transform ${breakdown ? "translate-x-3.5" : "translate-x-0.5"}`}
            />
          </span>
        </button>
      </div>
    </div>
  );
}

function niceCeil(max: number): number {
  if (max <= 0) return 0;
  const pow = Math.pow(10, Math.floor(Math.log10(max)));
  const norm = max / pow;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * pow;
}
