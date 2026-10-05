/**
 * Unified console — one state model over the single /api surface.
 * Surfaces: Projects, Sessions, Conversation, Activity, Changes, Files,
 * Child Sessions, Connections, Settings. No hosted/prototype layering.
 */

import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useUnified, UnifiedProvider } from "../state/unified";
import { LoginPage } from "./LoginPage";
import { SetupPage } from "./SetupPage";
import { SessionsPage } from "./SessionsPage";
import { NewSessionPage } from "./NewSessionPage";
import { SessionDetailPage } from "./SessionDetailPage";
import { ConnectionsPage } from "./ConnectionsPage";
import { SettingsPage } from "./SettingsPage";

function Shell({ children }: { children: React.ReactNode }) {
  const { state, signOut } = useUnified();
  return (
    <div className="shell">
      <a className="skip-link sr-only" href="#main">
        Skip to content
      </a>
      <nav className="side-nav" aria-label="Primary">
        <div className="top-bar">
          <strong>sbx</strong>
          <span className="faint small">{state.user?.email}</span>
        </div>
        <NavLink to="/sessions" className="nav-item">
          Sessions
        </NavLink>
        <NavLink to="/setup" className="nav-item">
          Setup
        </NavLink>
        <NavLink to="/connections" className="nav-item">
          Connections
        </NavLink>
        <NavLink to="/settings" className="nav-item">
          Settings
        </NavLink>
        <div className="grow" />
        <button className="btn btn-ghost btn-sm" onClick={() => void signOut()}>
          Sign out
        </button>
      </nav>
      <main className="main" id="main">
        <div className="main-inner">{children}</div>
      </main>
    </div>
  );
}

function Protected() {
  const { state, ready } = useUnified();
  if (!ready) {
    return (
      <main className="main">
        <p className="empty">Loading…</p>
      </main>
    );
  }
  if (!state.user) return <Navigate to="/login" replace />;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/sessions" replace />} />
        <Route path="/sessions" element={<SessionsPage />} />
        <Route path="/sessions/new" element={<NewSessionPage />} />
        <Route path="/sessions/:id" element={<SessionDetailPage />} />
        <Route path="/setup" element={<SetupPage />} />
        <Route path="/connections" element={<ConnectionsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<p className="empty">Not found</p>} />
      </Routes>
    </Shell>
  );
}

export function UnifiedApp() {
  return (
    <UnifiedProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/*" element={<Protected />} />
      </Routes>
    </UnifiedProvider>
  );
}
