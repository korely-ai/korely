// Predicate families + their accent colors, shared by the Graph and Memories
// views so a family reads the same color everywhere. Muted enough for a dev
// tool, distinct enough to tell apart at a glance.
//
// Which families exist, and in what order, is the engine's
// (memory_core/families.py FAMILY_ORDER), served by GET /admin/facts/families.
// This file held its own list of nine until the audit of 17 Oct 2026, without
// the engine's `identity`, so the filters could not select a rename or an
// alias. Only the colors live here: they are the screen's, and a family with
// none reads as "other" (tests/test_the_screen_keeps_one_truth.py checks that
// every family of the engine has one).
import { useEffect, useState } from "react";
import apiClient from "@/lib/apiClient";

export interface Family {
  name: string;
  label: string;
}

export const FAMILY_COLORS: Record<string, string> = {
  preferences: "#8b5cf6",
  people: "#ec4899",
  places: "#10b981",
  work: "#3b82f6",
  ownership: "#f59e0b",
  health: "#ef4444",
  financial: "#14b8a6",
  events: "#f97316",
  identity: "#6366f1",
  other: "#94a3b8",
};

export const familyColor = (f: string): string => FAMILY_COLORS[f] || FAMILY_COLORS.other;

// One read per page load, shared by every view that asks: the list changes
// only with a release of the server.
let served: Promise<Family[]> | null = null;

function readFamilies(): Promise<Family[]> {
  if (!served) {
    served = apiClient
      .get<{ families: Family[] }>("/admin/facts/families")
      .then((r) => r.data.families)
      .catch((e) => {
        served = null; // asked again next time, not remembered as empty
        throw e;
      });
  }
  return served;
}

/** The engine's families, in its order; empty until the server answers (the
 *  filters then offer "All families" alone, which is still true). */
export function useFamilies(): Family[] {
  const [families, setFamilies] = useState<Family[]>([]);
  useEffect(() => {
    let gone = false;
    readFamilies()
      .then((f) => !gone && setFamilies(f))
      .catch(() => {});
    return () => {
      gone = true;
    };
  }, []);
  return families;
}
