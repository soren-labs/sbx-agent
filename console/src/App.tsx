import { Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { IntegrationsPage } from "./pages/IntegrationsPage";
import { NewSessionPage } from "./pages/NewSessionPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { SessionDetailPage } from "./pages/SessionDetailPage";
import { SessionsPage } from "./pages/SessionsPage";
import { SettingsPage } from "./pages/SettingsPage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<NewSessionPage />} />
        <Route path="sessions" element={<SessionsPage />} />
        <Route path="sessions/:id" element={<SessionDetailPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
