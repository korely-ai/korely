import { lazy, Suspense, useEffect, useState } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  Outlet,
  useLocation,
} from "react-router";
import { AuthProvider } from "@/contexts/AuthContext";
import { InProjectScope, ProjectProvider } from "@/contexts/ProjectContext";
import { DashboardLayout } from "@/components/DashboardLayout";
import { RequireAuth } from "@/components/RequireAuth";
import { PageBoundary } from "@/components/PageBoundary";
import { LoginPage } from "@/pages/LoginPage";
import { SKIPPED } from "@/lib/setupSkipped";
import { modelsApi } from "@/lib/agentsApi";
import { ModeContext, loadMode, type Mode } from "@/contexts/ModeContext";
import { cloud, useScreenParts, type CloudScreen } from "@/lib/cloud";
import { unknownAddress } from "@/lib/unknownAddress";
import { lostAt } from "@/lib/returns";
import { ErrorNote } from "@/components/LoadState";

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


/** An address no route has: home, once the screen knows every route the
 *  account has (lib/unknownAddress.ts). Under RequireAuth, so it waits for
 *  the session and a person signed out is sent to the login with the
 *  address, which brings them back to it; and it waits for the parts the
 *  account is served (lib/cloud.ts, useScreenParts), whose routes are not
 *  there until their chunk is: a reload of a part's page, a link to it
 *  through the login, or a source's return to it went home before. On the
 *  address of a source's return the start could not finish, it says so
 *  (ReturnLost). */
function UnknownAddress() {
  const { ready } = useScreenParts();
  const { pathname } = useLocation();
  const where = unknownAddress(ready, lostAt(pathname));
  if (where === "lost") return <ReturnLost />;
  if (where === "wait") return <Loading />;
  return <Navigate to="/" replace />;
}

/** A source's return the start could not finish (lib/returns.ts): what
 *  the source sent back is gone from the address and was sent nowhere.
 *  Loading the screen again here asks the parts' chunk again, and a part
 *  whose return this is says on its page that nothing came back, where
 *  the person starts over. */
function ReturnLost() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <ErrorNote onRetry={() => window.location.reload()} className="max-w-md">
        What you were sent back with could not be finished: the screen did not load what finishes it in
        time. Nothing of it was sent to the server. Try again to load the screen here, then start over.
      </ErrorNote>
    </div>
  );
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
  // The Cloud's parts the signed-in account is served, as the server says
  // (lib/cloud.ts, useScreenParts): none in the public repository.
  const { parts } = useScreenParts();
  return (
    <Routes>
      <Route path="/login" element={cloud ? <cloud.LoginPage /> : <LoginPage />} />
      {/* The Cloud's sign-up view of the same page, where the site's "Get
          started" sends a newcomer. An install of one's
          own makes its accounts on the machine: there /signup is an
          address no route has, which leads to the sign-in. */}
      {cloud && <Route path="/signup" element={<cloud.LoginPage />} />}
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
          {/* The pages of the data, read in the project chosen at the top
              (or every project), and mounted again when it changes
              (contexts/ProjectContext.tsx). The others manage the account:
              their lists carry each row's project. */}
          <Route element={<InProjectScope />}>
            <Route index element={<HomePage />} />
            <Route path="/playground" element={<PlaygroundPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/memories" element={<MemoriesPage />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/facts" element={<FactsPage />} />
            <Route path="/requests" element={<RequestsPage />} />
            <Route path="/audit" element={<AuditPage />} />
            {/* The Cloud's parts of the product, on the hosted service alone
                and for an account the server serves them (lib/cloud.ts,
                useScreenParts): none in the public repository. An address
                of a part the account is not served goes home as an unknown
                address does, once the screen knows (UnknownAddress). Their data lives in a project too, so their pages read the
                one chosen. A part's own path alone: the addresses under it
                that a source outside sends a person back to never reach
                the router (lib/returns.ts, taken out in main.tsx), and any
                other goes home as an unknown address does. */}
            {hosted &&
              parts.map((p) => (
                <Route key={p.path} path={p.path} element={<p.Page />} />
              ))}
          </Route>
          <Route path="/install" element={<InstallPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/keys" element={<ApiKeysPage />} />
          <Route path="/webhooks" element={<WebhooksPage />} />
          <Route path="/usage" element={cloud ? <cloud.PlanPage /> : <UsagePage />} />
          {cloud && <Route path="/account" element={<cloud.AccountPage />} />}
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        </Route>
        <Route path="*" element={<UnknownAddress />} />
      </Route>
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
        {/* The last stand: a screen outside the dashboard's pages (the
            sign-in, Setup) that breaks says so instead of going white. The
            pages have their own, by address (DashboardLayout). */}
        <PageBoundary>
          <Auth>
            <BrowserRouter>
              <AppRoutes cloud={theCloud} />
            </BrowserRouter>
          </Auth>
        </PageBoundary>
      </Suspense>
    </ModeContext.Provider>
  );
}
