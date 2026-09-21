import React, { useEffect, useMemo, useState } from "react";
import { SCENARIOS, type Scenario } from "./mock";
import Overview from "./screens/Overview";
import Agents from "./screens/Agents";
import AgentDetail from "./screens/AgentDetail";
import CreateAgent from "./screens/CreateAgent";
import Providers from "./screens/Providers";
import Workspace from "./screens/Workspace";
import Workflows from "./screens/Workflows";
import Artifacts from "./screens/Artifacts";
import ApiKeys from "./screens/ApiKeys";
import Usage from "./screens/Usage";
import States from "./screens/States";

export interface Ctx {
  scenario: Scenario;
  nav: (path: string) => void;
}
export const AppCtx = React.createContext<Ctx>({ scenario: "live", nav: () => {} });

const NAV: { group: string; items: { path: string; label: string }[] }[] = [
  {
    group: "Operate",
    items: [
      { path: "/", label: "Overview" },
      { path: "/agents", label: "Agents" },
      { path: "/create", label: "Create agent" },
      { path: "/workflows", label: "Workflows" },
    ],
  },
  {
    group: "Assets",
    items: [
      { path: "/artifacts", label: "Artifacts" },
      { path: "/usage", label: "Usage & cost" },
    ],
  },
  {
    group: "Admin",
    items: [
      { path: "/providers", label: "Providers & accounts" },
      { path: "/keys", label: "API keys" },
      { path: "/states", label: "Edge states" },
    ],
  },
];

function parseHash(): { path: string; parts: string[] } {
  const h = window.location.hash.replace(/^#/, "") || "/";
  return { path: h, parts: h.split("/").filter(Boolean) };
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [scenario, setScenario] = useState<Scenario>("live");
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    const fn = () => setRoute(parseHash());
    window.addEventListener("hashchange", fn);
    return () => window.removeEventListener("hashchange", fn);
  }, []);

  const nav = (p: string) => {
    window.location.hash = p;
    setNavOpen(false);
  };

  const ctx = useMemo(() => ({ scenario, nav }), [scenario]);

  let screen: React.ReactNode;
  const p = route.parts;
  if (p[0] === "agents" && p[1] === "create") screen = <CreateAgent />;
  else if (p[0] === "agents" && p[1]) screen = <AgentDetail id={p[1]} />;
  else if (p[0] === "agents") screen = <Agents />;
  else if (p[0] === "create") screen = <CreateAgent />;
  else if (p[0] === "providers") screen = <Providers />;
  else if (p[0] === "workspace" && p[1]) screen = <Workspace id={p[1]} />;
  else if (p[0] === "workflows") screen = <Workflows id={p[1]} />;
  else if (p[0] === "artifacts") screen = <Artifacts id={p[1]} />;
  else if (p[0] === "keys") screen = <ApiKeys />;
  else if (p[0] === "usage") screen = <Usage />;
  else if (p[0] === "states") screen = <States />;
  else screen = <Overview />;

  const isActive = (path: string) =>
    path === "/" ? route.path === "/" : route.path.startsWith(path);

  return (
    <AppCtx.Provider value={ctx}>
      <div className="layout">
        {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}
        <aside className={`sidebar ${navOpen ? "open" : ""}`}>
          <div className="brand">
            sbx Control Center
            <small>prototype · SOR-171 · mock data</small>
          </div>
          {NAV.map((g) => (
            <div key={g.group}>
              <div className="nav-group">{g.group}</div>
              {g.items.map((it) => (
                <div
                  key={it.path}
                  className={`nav-item ${isActive(it.path) ? "active" : ""}`}
                  onClick={() => nav(it.path)}
                >
                  <span className="dot" />
                  {it.label}
                </div>
              ))}
            </div>
          ))}
        </aside>
        <div className="main">
          <div className="topbar">
            <button className="btn ghost sm menu-btn" onClick={() => setNavOpen(!navOpen)}>
              ☰
            </button>
            <span className="env">
              sbx.sorenforge.com <span className="muted">· v0.1.1 · /v1</span>
            </span>
            <span className="spacer" />
            <span className="small muted">Scenario:</span>
            <select
              value={scenario}
              onChange={(e) => setScenario(e.target.value as Scenario)}
              style={{
                background: "#0e121c",
                color: "var(--text)",
                border: "1px solid var(--border)",
                borderRadius: 7,
                padding: "6px 8px",
                fontSize: 12.5,
              }}
            >
              {SCENARIOS.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
            <button className="btn primary sm" onClick={() => nav("/agents/create")}>
              + Create agent
            </button>
          </div>
          <div className="content">{screen}</div>
        </div>
      </div>
    </AppCtx.Provider>
  );
}
