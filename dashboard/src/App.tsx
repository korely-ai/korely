import { lazy, Suspense, useEffect, useState } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  Outlet,
  useLocation,
} from "react-router";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { ProjectProvider } from "@/contexts/ProjectContext";
import { DashboardLayout } from "@/components/DashboardLayout";
import { LoginPage } from "@/pages/LoginPage";
import { SKIPPED } from "@/lib/setupSkipped";
import { modelsApi } from "@/lib/agentsApi";
import { ModeContext, loadMode, type Mode } from "@/contexts/ModeContext";
import { cloud, type CloudScreen } from "@/lib/cloud";

// Each page is its own chunk, fetched the first time it is opened: they all
// sat in the one main chunk, so the login downloaded the graph, the audit and
// the playground before anyone signed in (audit of 17 Oct 2026). The
// login stays in, being the first screen; DashboardLayout waits for a page
// under its own Suspense, so the sidebar stays while one loads.
const page = <K extends string>(load: () => Promise<Record<K, React.ComponentType>>, name: K) =>
  lazy(() => load().then((m) => ({ default: m[name] })));
const OAuthConsentPage = page(() => import("@/pages/OAuthConsentPage"), "OAuthConsentPage");
const HomePage = page(() => import("@/pages/HomePage"), "HomePage");
const PlaygroundPage = page(() => import("@/pages/PlaygroundPage"), "PlaygroundPage");
const InstallPage = page(() => import("@/pages/InstallPage"), "InstallPage");
const ProjectsPage = page(() => import("@/pages/ProjectsPage"), "ProjectsPage");
const ApiKeysPage = page(() => import("@/pages/ApiKeysPage"), "ApiKeysPage");
const MemoriesPage = page(() => import("@/pages/MemoriesPage"), "MemoriesPage");
const GraphPage = page(() => import("@/pages/GraphPage"), "GraphPage");
const AgentsPage = page(() => import("@/pages/AgentsPage"), "AgentsPage");
const FactsPage = page(() => import("@/pages/FactsPage"), "FactsPage");
const WebhooksPage = page(() => import("@/pages/WebhooksPage"), "WebhooksPage");
const RequestsPage = page(() => import("@/pages/RequestsPage"), "RequestsPage");
const AuditPage = page(() => import("@/pages/AuditPage"), "AuditPage");
const UsagePage = page(() => import("@/pages/UsagePage"), "UsagePage");
const SettingsPage = page(() => import("@/pages/SettingsPage"), "SettingsPage");
const SetupPage = page(() => import("@/pages/SetupPage"), "SetupPage");

// The hosted service's own pages (dashboard/src/hosted/), through lib/cloud.ts
// and only there: the sign-in with Firebase, its login, the plan and the
// account are lazy chunks, which the screen of an install of one's own never
// downloads. A few small pieces of hosted/ ship in every build that has the
// folder (the config that tells the two products apart, and the sidebar's and
// the home page's usage line with the client it reads through); they call the
// Cloud's routes only on the Cloud (audit of 17 Oct 2026; the comment
// used to say none of hosted/ was ever downloaded). A build without the folder
// (the public repository) has none of them.

function Loading() {
  return (
    <div className="flex h-full items-center justify-center text-muted-foreground">
      Loading…
    </div>
  );
}

/** Gate authenticated routes; bounce to /login while signed out. */
function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        Loading…
      </div>
    );
  }
  // Where they were going rides along, so the login brings them back to it.
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;
  return <Outlet />;
}

/** Until something writes the memories, the first login goes to /setup
 *  (since 2026-10-02): where, by which provider, with which key. An
 *  install configured in .env never sees it. */
function RequireWriter() {
  const [state, setState] = useState<"checking" | "ready" | "setup">("checking");
  useEffect(() => {
    modelsApi
      .get()
      .then((m) => {
        let skipped = false;
        try {
          skipped = localStorage.getItem(SKIPPED) === "1";
        } catch {
          // No storage: ask, which is the safe side.
        }
        setState(m.configured || skipped ? "ready" : "setup");
      })
      // An older server, or a key without the admin scope: never trap anyone.
      .catch(() => setState("ready"));
  }, []);
  if (state === "checking") {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        Loading…
      </div>
    );
  }
  if (state === "setup") return <Navigate to="/setup" replace />;
  return <Outlet />;
}

function AppRoutes({ cloud }: { cloud: CloudScreen | null }) {
  // On the hosted service no account is sent to choose a model (the service
  // has one, set by the superadmin), the login is a provider or an email,
  // /usage is the plan, and the account has a page of its own.
  const hosted = cloud !== null;
  return (
    <Routes>
      <Route path="/login" element={cloud ? <cloud.LoginPage /> : <LoginPage />} />
      <Route element={<RequireAuth />}>
        {/* On the hosted service only the superadmin reaches it from
            Settings, to set the service's models; nobody is sent there. */}
        <Route path="/setup" element={<SetupPage />} />
        {/* OAuth (16 Oct 2026): an app asks to connect; a person says yes. */}
        <Route path="/oauth/consent" element={<OAuthConsentPage />} />
        <Route element={hosted ? <Outlet /> : <RequireWriter />}>
        <Route
          element={
            <ProjectProvider>
              <DashboardLayout />
            </ProjectProvider>
          }
        >
          <Route index element={<HomePage />} />
          <Route path="/install" element={<InstallPage />} />
          <Route path="/playground" element={<PlaygroundPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/keys" element={<ApiKeysPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/memories" element={<MemoriesPage />} />
          <Route path="/graph" element={<GraphPage />} />
          <Route path="/facts" element={<FactsPage />} />
          <Route path="/webhooks" element={<WebhooksPage />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/usage" element={cloud ? <cloud.PlanPage /> : <UsagePage />} />
          {cloud && <Route path="/account" element={<cloud.AccountPage />} />}
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  // Which product this screen serves, asked once: only the hosted service
  // says so in /health (contexts/ModeContext.tsx).
  const [mode, setMode] = useState<Mode | null>(null);
  useEffect(() => {
    loadMode(cloud).then(setMode);
  }, []);
  if (!mode) return <Loading />;

  // `hosted` is true only in a build that has the Cloud's pieces (loadMode).
  const theCloud = mode.hosted ? cloud : null;
  const Auth = theCloud ? theCloud.AuthProvider : AuthProvider;
  return (
    <ModeContext.Provider value={mode}>
      <Suspense fallback={<Loading />}>
        <Auth>
          <BrowserRouter>
            <AppRoutes cloud={theCloud} />
          </BrowserRouter>
        </Auth>
      </Suspense>
    </ModeContext.Provider>
  );
}
