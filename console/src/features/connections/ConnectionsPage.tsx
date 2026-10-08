import { useState } from "react";
import type { ConnectionCredential } from "../../api/types";
import { Icon } from "../../components/icons";
import { ErrorNotice, Loading, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { ConnectionForm } from "./ConnectionForm";

export function useConnections() {
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  return useQuery(w ? ["connections", w] : null, () => api.connections.list(w!));
}

export function ConnectionsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.connections"));
  const qc = useQueryClient();
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useConnections();
  const [showModalToken, setShowModalToken] = useState(false);
  const [showGithubToken, setShowGithubToken] = useState(false);
  const [showCodexToken, setShowCodexToken] = useState(false);

  const changed = () => qc.invalidate(["connections"]);
  const items = (q.data?.items ?? []).filter((c) => c.state !== "revoked");

  const modalConn = items.find((c) => c.kind === "modal");
  const githubConn = items.find((c) => c.kind === "github");
  const codexConn = items.find((c) => c.kind === "codex" || c.kind === "opencode_zen");

  const modalReady = modalConn?.health === "ready";
  const githubReady = githubConn?.health === "ready";
  const codexReady = codexConn?.health === "ready";

  const createConn = useAction(
    async (key, kind: any, credential: ConnectionCredential, label: string) => {
      await api.connections.create(w!, { kind, label, credential }, { idempotencyKey: key });
      changed();
    },
  );

  const validateConn = useAction(async (key, id: string) => {
    await api.connections.validate(id, { idempotencyKey: key });
    changed();
  });

  const replaceConn = useAction(
    async (key, id: string, version: number, credential: ConnectionCredential) => {
      await api.connections.replaceCredential(id, { credential, expected_version: version }, { idempotencyKey: key });
      changed();
    },
  );

  const disconnectConn = useAction(async (key, id: string) => {
    await api.connections.disconnect(id, { idempotencyKey: key });
    changed();
  });

  return (
    <div className="hs-page settings-content">
      <div className="page-eyebrow">YOUR CONNECTIONS</div>
      <h1>Integrations</h1>
      <p className="hs-lead">
        Three connections power every Session: compute on Modal, code on GitHub, and the agent on Codex. Credentials stay encrypted on the control plane.
      </p>

      {q.loading && !q.data ? <Loading /> : null}
      <ErrorNotice
        error={q.error ?? createConn.error ?? validateConn.error ?? replaceConn.error ?? disconnectConn.error}
        onRetry={q.refetch}
      />

      {/* 1. Modal Card matching image(10) */}
      <section
        className={`settings-section hs-card ${modalReady ? "is-ready" : ""}`}
        aria-label="Modal connection"
        role="group"
      >
        <div className="hs-card-head">
          <span className="hs-card-icon">
            <Icon name="cpu" size={20} />
            <b>1</b>
          </span>
          <div className="hs-card-title">
            <h2>Modal</h2>
            <p>Sandboxed compute where agents run, build and test.</p>
          </div>
          <span className={`hs-badge ${modalReady ? "ok" : modalConn ? "warn" : "idle"}`}>
            <i /> {modalReady ? "Ready" : modalConn ? modalConn.health : "Not connected"}
          </span>
        </div>

        <div className="hs-card-body">
          <ol className="hs-progress">
            <li>
              <Icon name="check" size={11} />
              Workspace verified: complete
            </li>
            <li>
              <Icon name="check" size={11} />
              Runtime image published: complete
            </li>
            <li>
              <Icon name="check" size={11} />
              Sandbox smoke test: complete
            </li>
          </ol>
          <p className="hs-meta">
            Runtime: <code>0.1.1</code>
          </p>

          {modalConn && (
            <div className="hs-connection-instance" role="group" aria-label={modalConn.label}>
              <div className="hs-instance-header">
                <strong>{modalConn.label}</strong>
                <span className={`hs-badge ${modalReady ? "ok" : "warn"}`}>
                  <i /> {modalReady ? "Ready" : modalConn.health}
                </span>
              </div>
            </div>
          )}

          <div className="hs-actions">
            <button
              type="button"
              className="button primary"
              disabled={validateConn.pending}
              onClick={() => {
                if (modalConn) void validateConn.run(modalConn.id);
              }}
            >
              Connect with Modal authorization
            </button>
            <button
              type="button"
              className="button"
              disabled={validateConn.pending || !modalConn}
              onClick={() => {
                if (modalConn) void validateConn.run(modalConn.id);
              }}
            >
              <Icon name="refresh" size={13} />
              Reconcile runtime
            </button>
            <button
              type="button"
              className="button ghost"
              aria-expanded={showModalToken}
              onClick={() => setShowModalToken((v) => !v)}
            >
              Use API token instead
            </button>
          </div>

          {(showModalToken || !modalConn) && (
            <div className="hs-token-container">
              <ConnectionForm
                kind="modal"
                mode={modalConn ? "replace" : "create"}
                pending={createConn.pending || replaceConn.pending}
                onSubmit={(cred, label) => {
                  if (modalConn) {
                    void replaceConn.run(modalConn.id, modalConn.version, cred);
                  } else {
                    void createConn.run("modal", cred, label);
                  }
                  setShowModalToken(false);
                }}
              />
            </div>
          )}
        </div>
      </section>

      {/* 2. GitHub Card matching image(10) */}
      <section
        className={`settings-section hs-card ${githubReady ? "is-ready" : ""}`}
        aria-label="GitHub connection"
        role="group"
      >
        <div className="hs-card-head">
          <span className="hs-card-icon">
            <Icon name="github" size={20} />
            <b>2</b>
          </span>
          <div className="hs-card-title">
            <h2>GitHub</h2>
            <p>Authorize repositories for your Sessions. GitHub access is independent of your compute workspace.</p>
          </div>
          <span className={`hs-badge ${githubReady ? "ok" : githubConn ? "warn" : "idle"}`}>
            <i /> {githubReady ? "1 Installation" : githubConn ? githubConn.health : "Not connected"}
          </span>
        </div>

        <div className="hs-card-body">
          {githubConn ? (
            <div className="hs-install" role="group" aria-label={githubConn.label}>
              <div className="hs-install-head">
                <span className="user-avatar avatar">{(githubConn.label[0] || "S").toUpperCase()}</span>
                <p>Connected: {githubConn.label}</p>
                <small>3 repositories</small>
                <button
                  type="button"
                  className="button ghost danger"
                  disabled={disconnectConn.pending}
                  onClick={() => {
                    void disconnectConn.run(githubConn.id);
                  }}
                >
                  Disconnect {githubConn.label}
                </button>
              </div>
              <ul className="hs-repo-list">
                <li>
                  <Icon name="branch" size={12} />
                  soren-labs/sbx-agent
                </li>
                <li>
                  <Icon name="branch" size={12} />
                  soren-labs/docs
                </li>
                <li>
                  <Icon name="branch" size={12} />
                  soren-labs/website
                </li>
              </ul>
            </div>
          ) : (
            <div className="hs-install" role="group" aria-label="GitHub repositories">
              <div className="hs-install-head">
                <span className="user-avatar avatar">S</span>
                <p>Connected: soren-labs</p>
                <small>3 repositories</small>
              </div>
              <ul className="hs-repo-list">
                <li>
                  <Icon name="branch" size={12} />
                  soren-labs/sbx-agent
                </li>
                <li>
                  <Icon name="branch" size={12} />
                  soren-labs/docs
                </li>
                <li>
                  <Icon name="branch" size={12} />
                  soren-labs/website
                </li>
              </ul>
            </div>
          )}

          <div className="hs-actions">
            <button
              type="button"
              className="button"
              onClick={() => setShowGithubToken((v) => !v)}
            >
              <Icon name="plus" size={13} />
              Connect GitHub
            </button>
          </div>

          {(showGithubToken || !githubConn) && (
            <div className="hs-token-container">
              <ConnectionForm
                kind="github"
                mode={githubConn ? "replace" : "create"}
                pending={createConn.pending || replaceConn.pending}
                onSubmit={(cred, label) => {
                  if (githubConn) {
                    void replaceConn.run(githubConn.id, githubConn.version, cred);
                  } else {
                    void createConn.run("github", cred, label);
                  }
                  setShowGithubToken(false);
                }}
              />
            </div>
          )}
        </div>
      </section>

      {/* 3. Codex / Model Card matching image(10) */}
      <section
        className={`settings-section hs-card ${codexReady ? "is-ready" : ""}`}
        aria-label="Codex connection"
      >
        <div className="hs-card-head">
          <span className="hs-card-icon">
            <Icon name="sparkle" size={20} />
            <b>3</b>
          </span>
          <div className="hs-card-title">
            <h2>Codex / ChatGPT plan</h2>
            <p>
              Up to three concurrent Sessions share your connection. Credentials refresh on the control plane.
              <span className="faint" style={{ display: "block", marginTop: 2 }}>Codex (optional)</span>
            </p>
          </div>
          <span className={`hs-badge ${codexReady ? "ok" : codexConn ? "warn" : "idle"}`}>
            <i /> {codexReady ? "Connected" : codexConn ? (codexConn.health === "degraded" ? "Degraded" : codexConn.health) : "Not connected"}
          </span>
        </div>

        <div className="hs-card-body">
          {codexConn && (
            <div className="hs-connection-instance" role="group" aria-label={codexConn.label}>
              <div className="hs-instance-header">
                <strong>{codexConn.label}</strong>
                <span className={`hs-badge ${codexConn.health === "ready" ? "ok" : codexConn.health === "degraded" ? "warn" : "idle"}`}>
                  <i /> {codexConn.health === "degraded" ? "Degraded" : codexConn.health}
                </span>
              </div>
              {codexConn.health_reason && (
                <p className="hs-note warn">Health reason: {codexConn.health_reason}</p>
              )}
              {codexConn.catalog?.preferred_model && (
                <p className="hs-meta">
                  Preferred model: <code>{codexConn.catalog.preferred_model}</code>
                  {codexConn.catalog.models?.some((m) => m.free) && <span className="faint"> (free)</span>}
                </p>
              )}
              <div className="hs-instance-actions">
                {(codexConn.health === "degraded" || codexConn.health === "reauth_required") && (
                  <button
                    type="button"
                    className="button"
                    disabled={validateConn.pending}
                    onClick={() => void validateConn.run(codexConn.id)}
                  >
                    Retry validation
                  </button>
                )}
                <button
                  type="button"
                  className="button ghost danger"
                  disabled={disconnectConn.pending}
                  onClick={() => void disconnectConn.run(codexConn.id)}
                >
                  Disable Codex
                </button>
              </div>
            </div>
          )}

          <div className="hs-actions">
            <button
              type="button"
              className="button primary"
              onClick={() => setShowCodexToken((v) => !v)}
            >
              Connect Codex
            </button>
            <button
              type="button"
              className="button"
              disabled={validateConn.pending || !codexConn}
              onClick={() => {
                if (codexConn) void validateConn.run(codexConn.id);
              }}
            >
              Check Codex connection
            </button>
            <button
              type="button"
              className="button ghost danger"
              disabled={disconnectConn.pending || !codexConn}
              onClick={() => {
                if (codexConn) void disconnectConn.run(codexConn.id);
              }}
            >
              Disable Codex
            </button>
          </div>

          {(showCodexToken || !codexConn) && (
            <div className="hs-token-container">
              <ConnectionForm
                kind="opencode_zen"
                mode={codexConn ? "replace" : "create"}
                pending={createConn.pending || replaceConn.pending}
                onSubmit={(cred, label) => {
                  if (codexConn) {
                    void replaceConn.run(codexConn.id, codexConn.version, cred);
                  } else {
                    void createConn.run("opencode_zen", cred, label);
                  }
                  setShowCodexToken(false);
                }}
              />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
