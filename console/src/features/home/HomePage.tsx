import { Link } from "react-router-dom";
import { Icon, Mark } from "../../components/icons";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useDocumentTitle } from "../../state/title";
import { useConnections } from "../connections/ConnectionsPage";
import { NewSession } from "../sessions/NewSession";
import { useSessionList } from "../sessions/SessionsPage";
import type { Session } from "../../api/types";

function greeting(email?: string) {
  const hour = new Date().getHours();
  const part =
    hour < 5
      ? "Working late"
      : hour < 12
        ? "Good morning"
        : hour < 18
          ? "Good afternoon"
          : "Good evening";
  const name = email ? email.split("@")[0] : null;
  return name ? `${part}, ${name}` : part;
}

function formatRelativeTime(dateStr?: string) {
  if (!dateStr) return "Just now";
  const ms = Date.now() - new Date(dateStr).getTime();
  const mins = Math.max(0, Math.floor(ms / 60_000));
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function HomePage() {
  const { t } = useI18n();
  const { me } = useAuth();
  useDocumentTitle(t("nav.home"));
  const recent = useSessionList({ lifecycle: "open", role: "" }, 4);
  const connections = useConnections();

  const connItems = connections.data?.items ?? [];
  const modalDone = connItems.some((c) => c.kind === "modal" && c.health === "ready");
  const githubDone = connItems.some((c) => c.kind === "github" && c.health === "ready");
  const modelDone = connItems.some(
    (c) => (c.kind === "codex" || c.kind === "opencode_zen") && c.health === "ready",
  );

  const doneCount = (modalDone ? 1 : 0) + (githubDone ? 1 : 0) + (modelDone ? 1 : 0);

  const steps = [
    {
      id: "modal",
      done: modalDone,
      title: "Connect Modal",
      text: "Compute for your sandboxes",
    },
    {
      id: "github",
      done: githubDone,
      title: "Connect GitHub",
      text: "Repositories agents can work on",
    },
    {
      id: "codex",
      done: modelDone,
      title: "Connect Codex",
      text: "The agent that writes the code",
    },
  ];

  return (
    <div className="home-content">
      {/* Centered Hero Greeting matching image(9) */}
      <div className="home-hero">
        <span className="home-hero-mark">
          <Mark />
        </span>
        <h1>{greeting(me?.user.email)}</h1>
        <p>What should your agents work on next?</p>
      </div>

      {/* Setup Progress Card across top matching image(9) */}
      <section className="setup-card" aria-label="Finish setting up your workspace">
        <div className="setup-card-head">
          <div>
            <h2>Finish setting up your workspace</h2>
            <p>
              {doneCount} of 3 connected · Sessions start once all three are ready.
            </p>
          </div>
          <span className="setup-ring" style={{ ["--p" as string]: doneCount / 3 }}>
            {doneCount}/3
          </span>
        </div>
        <div className="setup-steps">
          {steps.map((step) => (
            <Link
              key={step.id}
              to="/connections"
              className={`setup-step ${step.done ? "done" : ""}`}
            >
              <span className="setup-step-check">
                {step.done ? <Icon name="check" size={11} /> : null}
              </span>
              <span>
                <strong>{step.title}</strong>
                <small>{step.done ? "Connected" : step.text}</small>
              </span>
              {!step.done && <Icon name="arrowRight" size={13} />}
            </Link>
          ))}
        </div>
      </section>

      {/* Large Prompt-First Composer */}
      <NewSession />

      {/* Recent Sessions Cards Grid matching image(9) */}
      {recent.items.length > 0 && (
        <section className="home-recent" aria-label="Recent sessions">
          <div className="home-recent-head">
            <h2>Recent sessions</h2>
            <Link to="/sessions">
              View all <Icon name="arrowRight" size={12} />
            </Link>
          </div>
          <div className="home-recent-grid">
            {recent.items.slice(0, 4).map((s: Session) => {
              const active = s.lifecycle === "open" && s.activity === "running";
              const failed = s.activity === "failed";
              const isPr = s.title.toLowerCase().includes("pr") || s.labels?.includes("pr");
              const tone = active ? "ok" : failed ? "warn" : isPr ? "ok" : "idle";
              const label = active
                ? "Working"
                : failed
                  ? "Needs attention"
                  : isPr
                    ? "PR ready"
                    : "Idle";

              return (
                <Link key={s.id} to={`/sessions/${s.id}`} className="recent-card">
                  <span className="recent-card-top">
                    <span className={`hs-badge ${tone}`}>
                      <i /> {label}
                    </span>
                    <small>{formatRelativeTime(s.updated_at || s.created_at)}</small>
                  </span>
                  <strong>{s.title}</strong>
                  <small className="recent-card-repo">
                    <Icon name="branch" size={11} />
                    {s.worktree?.repository || "soren-labs/sbx-agent"}
                  </small>
                </Link>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
