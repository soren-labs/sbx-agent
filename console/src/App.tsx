import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { LoginPage, RegisterPage, VerifyEmailPage } from "./features/auth/AuthPages";
import { ConnectionsPage } from "./features/connections/ConnectionsPage";
import { HomePage } from "./features/home/HomePage";
import { MachinesPage } from "./features/machines/MachinesPage";
import { ProjectsPage } from "./features/projects/ProjectsPage";
import { SessionPage } from "./features/sessions/SessionPage";
import { SessionsPage } from "./features/sessions/SessionsPage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { useI18n } from "./i18n";
import { AppShell } from "./shell/AppShell";
import { useAuth } from "./state/auth";
import { EmptyState } from "./ui/EmptyState";

function RequireAuth() {
  const { status } = useAuth();
  const loc = useLocation();
  if (status === "loading") {
    return (
      <div className="app-loading" role="status" aria-label="SBX Agent Console">
        <span className="app-loading__mark">S</span>
      </div>
    );
  }
  if (status === "anonymous") return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />;
  return <Outlet />;
}

function NotFound() {
  const { t } = useI18n();
  return <EmptyState title={t("common.not_found")} />;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/verify-email" element={<VerifyEmailPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route index element={<HomePage />} />
          <Route path="sessions" element={<SessionsPage />} />
          <Route path="sessions/:id/:tab?" element={<SessionPage />} />
          <Route path="projects" element={<ProjectsPage />} />
          <Route path="machines" element={<MachinesPage />} />
          <Route path="connections" element={<ConnectionsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
