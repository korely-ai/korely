import { fieldClass } from "@/components/PageShell";
import type { Family } from "@/lib/families";

/**
 * The filters over the facts, shared by the Facts page and the Graph: one
 * end user, one family, and whether the closed facts come too. The two pages
 * wrote the same bar by hand (audit of 17 Oct 2026); only what the history
 * box means differs, so its label is the caller's.
 */
export function FactFilters({
  endUser,
  onEndUser,
  onApply,
  family,
  onFamily,
  families,
  history,
  onHistory,
  historyLabel,
  className = "",
}: {
  endUser: string;
  onEndUser: (value: string) => void;
  /** Enter in the end-user field: read again now. */
  onApply: () => void;
  family: string;
  onFamily: (value: string) => void;
  families: Family[];
  history: boolean;
  onHistory: (value: boolean) => void;
  historyLabel: string;
  className?: string;
}) {
  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>
      <input
        value={endUser}
        onChange={(e) => onEndUser(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && onApply()}
        placeholder="Filter by end user…"
        aria-label="End user"
        className={`w-48 ${fieldClass}`}
      />
      <select
        value={family}
        onChange={(e) => onFamily(e.target.value)}
        aria-label="Family"
        className={fieldClass}
      >
        <option value="">All families</option>
        {families.map((f) => (
          <option key={f.name} value={f.name}>{f.label}</option>
        ))}
      </select>
      <label className="flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
        <input
          type="checkbox"
          checked={history}
          onChange={(e) => onHistory(e.target.checked)}
          className="h-4 w-4 rounded border-input"
        />
        {historyLabel}
      </label>
    </div>
  );
}
