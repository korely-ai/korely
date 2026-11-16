import type { ReactNode } from "react";
import { Waypoints } from "lucide-react";
import { EmptyState } from "@/components/PageShell";
import { Loaded } from "@/components/LoadState";
import type { Load } from "@/lib/useLoad";

/**
 * What the graph draws for its read (lib/useLoad.ts): a failure is the
 * server's sentence with Try again, and the empty state comes only from an
 * answer with no facts. The read was caught by hand into an empty graph, so
 * a read that failed drew "No graph yet ... Add a memory or two" under the
 * error (pre-tester check of 15 Nov 2026). `children` draws the graph.
 *
 * Its own module, not the page's: the page loads sigma, which reads
 * WebGL2RenderingContext as it loads, so node cannot load the page, and
 * pages/FactsPage.behaviour.tsx draws this with a read that failed.
 */
export function GraphShown({
  load,
  shown,
  children,
}: {
  load: Load<unknown>;
  shown: number;
  children: () => ReactNode;
}) {
  return (
    <Loaded load={load} className="mb-4">
      {() =>
        shown === 0 ? (
          <EmptyState icon={<Waypoints className="h-6 w-6" />} title="No graph yet">
            As your agents store memories, Korely extracts typed facts and links
            them here. This is the differentiator: a temporal knowledge graph, not
            a flat list. Add a memory or two and it fills in.
          </EmptyState>
        ) : (
          children()
        )
      }
    </Loaded>
  );
}
