import { Suspense, useEffect, useId, useState } from "react";
import { Outlet, useLocation } from "react-router";
import { LogOut, Menu, Moon, Sun } from "lucide-react";
import { ModalShell } from "@/components/Modal";
import { Sidebar } from "@/components/Sidebar";
import { ProjectSwitcher } from "@/components/ProjectSwitcher";
import { useAuth } from "@/contexts/AuthContext";
import { useTheme } from "@/contexts/ThemeContext";

/**
 * Two-column shell: the sidebar and a scrollable main with a thin top bar.
 *
 * Below the `md` breakpoint (768 px) the sidebar is a drawer behind a menu
 * button, over the page, and the main takes the whole width: at 390 px the
 * 240 px sidebar stayed on screen, the content had about 150 px and the page
 * scrolled sideways to 560 px (functional test of 28 Oct 2026).
 *
 * The drawer is an overlay like every other (components/Modal.tsx,
 * `ModalShell`): a dialog out of `#root`, which is inert while it is open,
 * so the focus moves into it and stays there; Escape closes it and the focus
 * goes back to the menu button (review of 28 Oct 2026: it was a popover, and
 * the page behind it stayed in the Tab order). A press on the backdrop and
 * the choice of a page close it too.
 */
export function DashboardLayout() {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const [menu, setMenu] = useState(false);
  const drawerId = useId();
  const { pathname } = useLocation();
  useEffect(() => setMenu(false), [pathname]);
  return (
    <div className="flex h-full">
      <div className="hidden md:flex">
        <Sidebar />
      </div>
      {menu && (
        <ModalShell
          id={drawerId}
          label="Menu"
          onClose={() => setMenu(false)}
          className="fixed inset-0 z-50 md:hidden"
        >
          <div className="absolute inset-0 bg-black/40" aria-hidden="true" onMouseDown={() => setMenu(false)} />
          <div className="relative h-full w-60 shadow-xl">
            <Sidebar onNavigate={() => setMenu(false)} />
          </div>
        </ModalShell>
      )}
      <div className="flex flex-1 flex-col min-w-0">
        <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-border px-4 md:gap-3 md:px-6">
          <div className="flex min-w-0 items-center gap-2">
            <button
              type="button"
              onClick={() => setMenu(true)}
              aria-label="Open the menu"
              aria-expanded={menu}
              aria-controls={drawerId}
              className="inline-flex items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground md:hidden"
            >
              <Menu className="h-5 w-5" />
            </button>
            {/* Active-project switcher — top-left, beside the sidebar brand mark. */}
            <ProjectSwitcher />
          </div>
          <div className="flex shrink-0 items-center gap-1 sm:gap-3">
            <button
              type="button"
              onClick={toggleTheme}
              aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              title={theme === "dark" ? "Light mode" : "Dark mode"}
              className="inline-flex items-center justify-center rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
            >
              {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>
            <span className="hidden text-sm text-muted-foreground sm:inline">
              {user?.name}
            </span>
            <button
              type="button"
              onClick={() => void logout()}
              aria-label="Sign out"
              className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
            >
              <LogOut className="h-4 w-4" />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto px-4 py-6 md:px-8 md:py-8">
          <div className="mx-auto max-w-5xl">
            {/* A page is fetched the first time it opens (App.tsx): the
                shell stays while it loads. */}
            <Suspense
              fallback={<div className="py-12 text-center text-sm text-muted-foreground">Loading…</div>}
            >
              <Outlet />
            </Suspense>
          </div>
        </main>
      </div>
    </div>
  );
}
