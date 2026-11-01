// Where a project's memories are read by a model (services/regions.py on the
// server, 8 Oct 2026): "eu" writes with the provider saved for Europe on the
// Settings page, "global" with the one saved for Global. A project that never
// chose follows this install's default provider, the behaviour of before.
// On the Cloud the providers are the service's, set in its configuration and
// refused to an account (403 set_by_the_service): the words below say so
// there, instead of sending a customer to a Settings page that cannot do it
// (audit of 17 Oct 2026).
//
// "Follow" is an install's choice only. The Cloud stores a project made or
// reset without a region as Global, its default (services/projects._region_of),
// so it no longer offers to follow the service's default, a value it cannot
// store: there the empty choice reads as Global, which is what sending it
// stores (audit of 19 Oct 2026).
//
// A project already stored with none (the Cloud, made before 0.1.42) is not
// Global: its memories go to the service's default provider, as
// services/regions.of_project reads it, and only a write stores Global. So the
// select of such a project shows that, "Service default", and a save that
// leaves the choice alone sends no region (audit of 27 Oct 2026).
import { useMode } from "@/contexts/ModeContext";
import type { Region } from "@/lib/agentsApi";
import { fieldClass } from "@/components/PageShell";

export const REGION_LABEL: Record<Region, string> = {
  eu: "Europe",
  global: "Global",
};

export function RegionSelect({
  value,
  onChange,
  disabled,
  noneStored,
}: {
  value: Region | null;
  onChange: (region: Region | null) => void;
  disabled?: boolean;
  /** The project is stored with no region (Cloud, before 0.1.42). A new
   *  project made without one is stored Global, so it is not this. */
  noneStored?: boolean;
}) {
  const { hosted } = useMode();
  const follows = !hosted || noneStored;
  return (
    <select
      value={value ?? (follows ? "" : "global")}
      disabled={disabled}
      onChange={(e) => onChange((e.target.value || null) as Region | null)}
      className={`w-full ${fieldClass}`}
    >
      {follows && (
        <option value="">{hosted ? "Service default (no region stored)" : "Follow this install"}</option>
      )}
      <option value="eu">{REGION_LABEL.eu}</option>
      <option value="global">{REGION_LABEL.global}</option>
    </select>
  );
}

const CHANGING =
  "Changing it applies to the next memories and extracts nothing again, but " +
  "each new memory is read next to the same person's earlier memories and " +
  "facts, which then go to the new region's provider too.";

/** What the region choice means, on this install or on the Cloud. */
export function RegionHelp({ className }: { className?: string }) {
  const { hosted } = useMode();
  return (
    <p className={className}>
      {hosted
        ? "Where this project's memories are read by a model: Europe uses the " +
          "service's provider for Europe, Global the one for Global. " +
          CHANGING +
          " A region the service has no provider for keeps its memories waiting."
        : "Where this project's memories are read by a model. Europe uses the " +
          "provider you saved for Europe in Settings, Global the one for Global. " +
          CHANGING +
          " A region with no provider keeps its memories waiting until you add one."}
    </p>
  );
}

/** Where a project with no region of its own writes. On the Cloud only a
 *  project stored before 0.1.42 can have none. */
export function followsLabel(hosted: boolean): string {
  return hosted
    ? "No region stored (made before 0.1.42): follows the service's default provider"
    : "Follows this install";
}

/** Why a project's memories wait: no provider for its region. */
export function waitingReason(hosted: boolean): string {
  return hosted
    ? "Its memories wait until the service has a provider for this region."
    : "Its memories wait until a provider is saved for this region in Settings.";
}
