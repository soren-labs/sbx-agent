import { useEffect, useRef, useState } from "react";
import { useApi } from "../state/api";
import { connections, type AccountConnection } from "./domain";
import { demoMode, demoModels, providerNames } from "./demo";
import { Icon } from "./Icon";

export function Integrations() {
  const api = useApi();
  const [accounts, setAccounts] = useState<AccountConnection[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [modelRefresh, setModelRefresh] = useState("");
  const [synced, setSynced] = useState(
    demoMode ? "Synced 3 minutes ago" : "Installation status",
  );
  const [githubLabel, setGithubLabel] = useState(
    demoMode ? "soren-labs · 3 repositories available" : "GitHub App connected",
  );
  const [github, setGithub] = useState(false);
  const [provider, setProvider] = useState("codex");
  const [label, setLabel] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [target, setTarget] = useState<string>();
  const [pair, setPair] = useState<Awaited<
    ReturnType<typeof connections.begin>
  > | null>(null);
  const labelRef = useRef<HTMLInputElement>(null);
  const refresh = async () => {
    try {
      setAccounts(await connections.list());
    } catch (e) {
      setError(String((e as Error).message));
    }
  };
  useEffect(() => {
    void refresh();
    void api
      .getIntegrations()
      .then((s) => {
        setGithub(s.github.connected);
        if (!demoMode && s.github.accounts.length)
          setGithubLabel(s.github.accounts.join(" · "));
      })
      .catch(() => setGithub(false));
  }, [api]);
  useEffect(() => {
    if (connecting) labelRef.current?.focus();
  }, [connecting]);
  useEffect(() => {
    if (accounts.length) connections.rememberDemo(accounts);
  }, [accounts]);
  useEffect(() => {
    if (!pair || demoMode) return;
    const timer = setInterval(() => {
      void connections
        .poll(pair.id)
        .then((p) => {
          setPair(p);
          if (["verified"].includes(p.state)) {
            setConnecting(false);
            setPair(null);
            void refresh();
          }
        })
        .catch((e) => setError(e.message));
    }, 2500);
    return () => clearInterval(timer);
  }, [pair?.id]);
  const close = () => {
    if (pair)
      void connections.cancel(pair.id).catch((e) => setError(e.message));
    setPair(null);
    setConnecting(false);
  };
  const check = async (a: AccountConnection, action: "verify" | "refresh") => {
    setBusy(a.id + action);
    setError("");
    try {
      await connections.check(a.id, action);
      if (demoMode)
        setAccounts((list) =>
          list.map((row) =>
            row.id === a.id
              ? {
                  ...row,
                  lastVerified: "Just now",
                  health:
                    row.health === "needs_login" ? "needs_login" : "ready",
                }
              : row,
          ),
        );
      else await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const openConnect = (a?: AccountConnection) => {
    setProvider(a?.provider ?? "codex");
    setLabel(a?.label ?? "");
    setTarget(a?.id);
    setPair(null);
    setConnecting(true);
  };
  const begin = async () => {
    setBusy("connect");
    try {
      setPair(
        await connections.begin(
          provider,
          label || `${providerNames[provider]} account`,
          target,
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const completeDemo = () => {
    if (!demoMode) return;
    setAccounts((list) =>
      target
        ? list.map((a) =>
            a.id === target
              ? { ...a, health: "ready", lastVerified: "Just now" }
              : a,
          )
        : [
            ...list,
            {
              id: `demo-${Date.now()}`,
              provider,
              label: label || `${providerNames[provider]} account`,
              health: "ready",
              lastVerified: "Just now",
              models: demoModels[provider],
            },
          ],
    );
    setConnecting(false);
    setPair(null);
  };
  return (
    <div className="page-scroll">
      <div className="integrations-content">
        <div className="page-eyebrow">WORKSPACE</div>
        <div className="page-title-row">
          <div>
            <h1>Connections</h1>
            <p className="page-subtitle">
              Manage provider accounts and your GitHub connection.
            </p>
          </div>
          <button className="button primary" onClick={() => openConnect()}>
            <Icon name="plus" size={15} />
            Connect account
          </button>
        </div>
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button onClick={() => setError("")} aria-label="Dismiss error">
              <Icon name="x" />
            </button>
          </div>
        )}
        <div className="section-header">
          <h2>Source control</h2>
          <span>Repositories and pull requests</span>
        </div>
        <section className="github-card">
          <span className="integration-logo github-logo">
            <Icon name="github" size={25} />
          </span>
          <div>
            <h3>
              GitHub{" "}
              <span
                className={`status ${github ? "status-idle" : "status-failed"}`}
              >
                <span className="status-dot" />
                {github ? "Connected" : "Not connected"}
              </span>
            </h3>
            <p>
              {github
                ? githubLabel
                : "Connect your repositories to create and review pull requests."}
            </p>
            <small>{synced}</small>
          </div>
          <button
            className="button"
            disabled={busy === "github"}
            onClick={() => {
              setBusy("github");
              void (
                github
                  ? connections.syncGithub()
                  : demoMode
                    ? Promise.resolve()
                    : api.beginGithubAuthorize().then((r) => {
                        window.open(r.url, "_blank", "noopener,noreferrer");
                      })
              )
                .then(() => {
                  if (demoMode) setGithub(true);
                  setSynced(
                    github
                      ? "Synced just now"
                      : demoMode
                        ? "Connected in demo"
                        : "Complete installation in GitHub, then sync",
                  );
                })
                .catch((e) => setError(e.message))
                .finally(() => setBusy(""));
            }}
          >
            <Icon name="refresh" size={14} />
            {busy === "github"
              ? "Syncing…"
              : github
                ? "Sync repositories"
                : "Connect GitHub"}
          </button>
        </section>
        <div className="section-header">
          <h2>AI providers</h2>
          <span>{accounts.length} subscription accounts</span>
        </div>
        <p className="section-description">
          Use your existing subscriptions. SBX picks a ready account when you
          start a session.
        </p>
        <div className="provider-list">
          {Object.entries(providerNames).map(([id, name]) => {
            const rows = accounts.filter((a) => a.provider === id);
            const healthy = rows.some((a) => a.health === "ready");
            return (
              <section key={id} className="provider-card">
                <div className="provider-card-head">
                  <span className={`integration-logo provider-${id}`}>
                    {id === "codex" ? (
                      <Icon name="code" size={23} />
                    ) : id === "devin" ? (
                      "D"
                    ) : id === "antigravity" ? (
                      "A"
                    ) : id === "grok" ? (
                      "𝕏"
                    ) : (
                      <Icon name="terminal" size={22} />
                    )}
                  </span>
                  <div>
                    <h3>{name}</h3>
                    <p>
                      {id === "codex"
                        ? "OpenAI subscription"
                        : id === "devin"
                          ? "Autonomous software engineer"
                          : id === "antigravity"
                            ? "Google AI subscription"
                            : id === "grok"
                              ? "xAI subscription"
                              : "OpenCode Zen subscription"}
                    </p>
                  </div>
                  <span
                    className={`status ${healthy ? "status-idle" : "status-failed"}`}
                  >
                    <span className="status-dot" />
                    {healthy ? "Ready" : "Needs login"}
                  </span>
                  <button
                    className="icon-button"
                    aria-label={`Refresh ${name} models`}
                    title={
                      modelRefresh === id
                        ? "Models refreshed"
                        : "Refresh model catalog"
                    }
                    disabled={!!busy}
                    onClick={async () => {
                      setBusy(id + "models");
                      setError("");
                      try {
                        await connections.refreshModels(id);
                        setModelRefresh(id);
                      } catch (e) {
                        setError((e as Error).message);
                      } finally {
                        setBusy("");
                      }
                    }}
                  >
                    <Icon
                      name={modelRefresh === id ? "check" : "refresh"}
                      size={14}
                    />
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`Add ${name} account`}
                    onClick={() => {
                      openConnect();
                      setProvider(id);
                    }}
                  >
                    <Icon name="plus" />
                  </button>
                </div>
                <div className="account-table">
                  {rows.map((a) => (
                    <div className="account-row" key={a.id}>
                      <span className="account-avatar">{a.label[0]}</span>
                      <div className="account-name">
                        <strong>{a.label}</strong>
                        <span>{a.models.join(" · ")}</span>
                      </div>
                      <div className="account-health">
                        <span
                          className={
                            a.health === "ready" ? "positive" : "attention"
                          }
                        >
                          <span className="status-dot" />
                          {a.health === "ready"
                            ? "Healthy"
                            : a.health === "disabled"
                              ? "Disabled"
                              : "Reconnect needed"}
                        </span>
                        <small>{a.lastVerified}</small>
                      </div>
                      <div className="account-actions">
                        {a.health === "needs_login" ? (
                          <button
                            className="button attention-button"
                            onClick={() => openConnect(a)}
                          >
                            Reconnect
                          </button>
                        ) : (
                          <>
                            <button
                              className="button small"
                              disabled={!!busy}
                              onClick={() => void check(a, "verify")}
                            >
                              {busy === a.id + "verify"
                                ? "Checking…"
                                : "Verify"}
                            </button>
                            <button
                              className="icon-button"
                              disabled={!!busy}
                              aria-label={`Refresh ${a.label}`}
                              title="Refresh credentials"
                              onClick={() => void check(a, "refresh")}
                            >
                              <Icon name="refresh" size={14} />
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                  {!rows.length && (
                    <button
                      className="text-button"
                      onClick={() => {
                        openConnect();
                        setProvider(id);
                      }}
                    >
                      Connect your first {name} account
                      <Icon name="plus" size={13} />
                    </button>
                  )}
                </div>
              </section>
            );
          })}
        </div>
        <div className="integration-note">
          <Icon name="shield" size={19} />
          <p>
            <strong>Connections stay private.</strong> Each session receives
            only the account it needs. Credential health and refresh are managed
            here.
            {demoMode && (
              <small>
                Prototype mode: account actions are simulated locally.
              </small>
            )}
          </p>
        </div>
      </div>
      {connecting && (
        <div
          className="modal-backdrop"
          onKeyDown={(e) => {
            if (e.key === "Escape") close();
            if (e.key === "Tab") {
              const elements = Array.from(
                e.currentTarget.querySelectorAll<HTMLElement>(
                  "button:not(:disabled), input, select, a[href]",
                ),
              );
              const first = elements[0],
                last = elements.at(-1);
              if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last?.focus();
              } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first?.focus();
              }
            }
          }}
        >
          <section
            className="connect-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="connect-title"
          >
            <button
              className="icon-button modal-close"
              onClick={close}
              aria-label="Close connection dialog"
            >
              <Icon name="x" />
            </button>
            <span className="connect-symbol">
              <Icon name="plug" size={25} />
            </span>
            <h2 id="connect-title">
              {pair
                ? "Authorize your account"
                : target
                  ? "Reconnect your account"
                  : "Connect an AI account"}
            </h2>
            <p>
              {pair
                ? demoMode
                  ? "This demonstrates the secure pairing step. No login or credentials are needed."
                  : "Complete the provider’s sign-in flow. SBX will check the connection automatically."
                : "Bring your subscription. Sign in securely through your provider."}
            </p>
            {!pair ? (
              <>
                <label className="form-label">
                  Provider
                  <select
                    value={provider}
                    onChange={(e) => setProvider(e.target.value)}
                    disabled={!!target}
                  >
                    {Object.entries(providerNames).map(([id, name]) => (
                      <option key={id} value={id}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="form-label">
                  Account name
                  <input
                    ref={labelRef}
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder="e.g. Soren · work"
                  />
                </label>
                <button
                  className="button primary full"
                  disabled={!!busy}
                  onClick={() => void begin()}
                >
                  {busy ? "Connecting…" : "Continue securely"}
                  <Icon name="external" size={14} />
                </button>
              </>
            ) : (
              <>
                <div className="pair-code">{pair.user_code ?? pair.state}</div>
                {pair.browser_url && (
                  <a
                    className="button primary full"
                    target="_blank"
                    rel="noreferrer"
                    href={pair.browser_url}
                  >
                    Open provider sign-in
                    <Icon name="external" size={14} />
                  </a>
                )}
                {pair.pair_command && (
                  <pre className="pair-command">{pair.pair_command}</pre>
                )}
                {demoMode ? (
                  <button
                    className="button primary full"
                    onClick={completeDemo}
                  >
                    Complete demo connection
                    <Icon name="check" size={15} />
                  </button>
                ) : (
                  <p className="muted">Waiting for authorization…</p>
                )}
              </>
            )}
            <small className="fine-print">
              {demoMode
                ? "Local demo · nothing is sent to a provider"
                : "Credentials are never pasted into the conversation."}
            </small>
          </section>
        </div>
      )}
    </div>
  );
}
