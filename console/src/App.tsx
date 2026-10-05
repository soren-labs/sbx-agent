import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "./api/client";
import type {
  Changeset,
  Connection,
  Delegation,
  Delivery,
  Event,
  Model,
  Project,
  Session,
  User,
} from "./api/types";
import { cache, initial, apply } from "./state/session";

type Surface = "Sessions" | "Projects" | "Connections" | "Settings";
function Form({
  onSubmit,
  children,
}: {
  onSubmit: (data: FormData) => Promise<void>;
  children: React.ReactNode;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setPending(true);
    setError("");
    const form = e.currentTarget;
    try {
      await onSubmit(new FormData(form));
      form.reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setPending(false);
      form
        .querySelectorAll<HTMLInputElement>("input[type=password]")
        .forEach((i) => {
          i.value = "";
        });
    }
  }
  return (
    <form onSubmit={(e) => void submit(e)}>
      <fieldset disabled={pending}>{children}</fieldset>
      {pending && <p role="status">Working…</p>}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </form>
  );
}
const value = (data: FormData, key: string) => String(data.get(key) ?? "");
export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [surface, setSurface] = useState<Surface>("Sessions");
  const [selected, setSelected] = useState<string | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState("");
  const wid = user?.workspace_ids[0];
  useEffect(() => {
    void api
      .me()
      .then((u) => {
        setUser(u);
        cache.setOwner(u.id);
      })
      .catch(() => {})
      .finally(() => setLoaded(true));
  }, []);
  const reload = useCallback(async () => {
    if (!wid) return;
    try {
      const [c, p, s] = await Promise.all([
        api.connections(wid),
        api.projects(wid),
        api.sessions(wid),
      ]);
      setConnections(c.items);
      setProjects(p.items);
      setSessions(s.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        cache.clear();
        setUser(null);
      } else setError(String(e));
    }
  }, [wid]);
  useEffect(() => {
    void reload();
    const timer = setInterval(() => void reload(), 3000);
    return () => clearInterval(timer);
  }, [reload]);
  if (!loaded)
    return (
      <main>
        <p role="status">Loading…</p>
      </main>
    );
  if (!user)
    return (
      <main className="login">
        <p className="eyebrow">SBX BROWSER</p>
        <h1>Your work, continued.</h1>
        <p>Sign in to manage Projects and durable Sessions.</p>
        <Form
          onSubmit={async (data) => {
            await api.login(value(data, "email"), value(data, "password"));
            const u = await api.me();
            cache.setOwner(u.id);
            setUser(u);
          }}
        >
          <label>
            Email
            <input name="email" type="email" autoComplete="username" required />
          </label>
          <label>
            Password
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
            />
          </label>
          <button type="submit">Sign in</button>
        </Form>
        <details>
          <summary>Create an account</summary>
          <Form
            onSubmit={async (data) => {
              await api.request("/api/auth/register", "POST", {
                email: value(data, "email"),
                password: value(data, "password"),
              });
              setError(
                "Account created. Verify your email using the operator-delivered link.",
              );
            }}
          >
            <label>
              Email
              <input name="email" type="email" required />
            </label>
            <label>
              Password
              <input name="password" type="password" minLength={8} required />
            </label>
            <button>Create account</button>
          </Form>
        </details>
        {error && <p role="status">{error}</p>}
      </main>
    );
  return (
    <div className="shell">
      <aside>
        <a className="brand" href="#" onClick={() => setSelected(null)}>
          SBX<span>Browser</span>
        </a>
        <nav aria-label="Main">
          {(
            ["Sessions", "Projects", "Connections", "Settings"] as Surface[]
          ).map((s) => (
            <button
              key={s}
              className={surface === s ? "selected" : ""}
              onClick={() => {
                setSurface(s);
                setSelected(null);
              }}
            >
              {s}
            </button>
          ))}
        </nav>
        <div className="account">
          <p>{user.email}</p>
          <button
            onClick={() =>
              void api.logout().then(() => {
                cache.clear();
                setUser(null);
                setSelected(null);
              })
            }
          >
            Sign out
          </button>
        </div>
      </aside>
      <main>
        <header>
          <p className="eyebrow">PERSONAL WORKSPACE</p>
          <h1>{selected ? "Session" : surface}</h1>
          <span className="badge">OpenCode · Modal</span>
        </header>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {selected ? (
          <Detail id={selected} open={setSelected} />
        ) : surface === "Sessions" ? (
          <>
            <section className="checklist">
              <h2>Minimum setup</h2>
              <p>{user.verified_at ? "✓" : "○"} Email and password</p>
              {(["modal", "github", "opencode_zen"] as const).map((kind) => (
                <p key={kind}>
                  {connections.some(
                    (c) =>
                      c.kind === kind &&
                      c.state === "configured" &&
                      c.health === "ready",
                  )
                    ? "✓"
                    : "○"}{" "}
                  {kind === "opencode_zen"
                    ? "OpenCode Zen"
                    : kind === "modal"
                      ? "Modal token ID / secret"
                      : "GitHub manual token"}
                </p>
              ))}
            </section>
            <Composer
              wid={wid!}
              connections={connections}
              projects={projects}
              created={(id) => {
                void reload();
                setSelected(id);
              }}
            />
            <section>
              <h2>Sessions</h2>
              <div className="cards">
                {sessions.map((s) => (
                  <button
                    className="card"
                    key={s.id}
                    onClick={() => setSelected(s.id)}
                  >
                    <strong>{s.title}</strong>
                    <span>
                      {s.lifecycle} · {s.provider_id}
                    </span>
                    <small>{s.id}</small>
                  </button>
                ))}
              </div>
              {!sessions.length && (
                <p>
                  No Sessions yet. Start with a Project and a coding request.
                </p>
              )}
            </section>
          </>
        ) : surface === "Connections" ? (
          <Connections wid={wid!} items={connections} reload={reload} />
        ) : surface === "Projects" ? (
          <>
            <Form
              onSubmit={async (data) => {
                await api.request(`/api/workspaces/${wid}/projects`, "POST", {
                  name: value(data, "name"),
                  repository: value(data, "repository"),
                  base_ref: value(data, "base") || "main",
                  spec: JSON.parse(value(data, "spec") || "{}"),
                });
                await reload();
              }}
            >
              <h2>Create Project</h2>
              <label>
                Name
                <input name="name" required />
              </label>
              <label>
                GitHub repository
                <input
                  name="repository"
                  type="url"
                  placeholder="https://github.com/owner/repository"
                  required
                />
              </label>
              <label>
                Base ref
                <input name="base" defaultValue="main" />
              </label>
              <label>
                Environment and services declarations (JSON)
                <textarea
                  name="spec"
                  defaultValue={'{"env":{},"setup":[],"services":[]}'}
                />
              </label>
              <button>Create immutable version</button>
            </Form>
            <section>
              <h2>Projects</h2>
              {projects.map((p) => (
                <article key={p.id}>
                  <h3>{p.name}</h3>
                  <p>
                    Version {p.version} · {p.current_version_id}
                  </p>
                </article>
              ))}
            </section>
          </>
        ) : (
          <Settings user={user} />
        )}
      </main>
    </div>
  );
}

function Composer({
  wid,
  connections,
  projects,
  created,
}: {
  wid: string;
  connections: Connection[];
  projects: Project[];
  created: (id: string) => void;
}) {
  const zen = connections.filter(
    (c) =>
      c.kind === "opencode_zen" &&
      c.state === "configured" &&
      c.health === "ready",
  );
  const [cid, setCid] = useState("");
  const [models, setModels] = useState<Model[]>([]);
  useEffect(() => {
    if (!cid && zen[0]) setCid(zen[0].id);
  }, [zen, cid]);
  useEffect(() => {
    setModels([]);
    if (cid) void api.models(cid).then((r) => setModels(r.models));
  }, [cid]);
  const modal = connections.filter(
    (c) =>
      c.kind === "modal" && c.state === "configured" && c.health === "ready",
  );
  const github = connections.filter(
    (c) =>
      c.kind === "github" && c.state === "configured" && c.health === "ready",
  );
  return (
    <Form
      onSubmit={async (data) => {
        const r = await api.request<{ session_id: string }>(
          `/api/workspaces/${wid}/sessions`,
          "POST",
          {
            title: value(data, "title") || "Coding session",
            project_version_id: value(data, "project") || undefined,
            provider_id: "opencode",
            backend: "modal",
            model: value(data, "model"),
            zen_connection_id: cid,
            modal_connection_id: value(data, "modal"),
            github_connection_id: value(data, "github"),
            message: { content: value(data, "prompt"), routing: "queue" },
          },
        );
        created(r.session_id);
      }}
    >
      <h2>Start a Session</h2>
      <div className="grid">
        <label>
          Title
          <input name="title" />
        </label>
        <label>
          Project
          <select name="project">
            <option value="">Projectless</option>
            {projects.map((p) => (
              <option key={p.id} value={p.current_version_id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          OpenCode Zen connection
          <select value={cid} onChange={(e) => setCid(e.target.value)}>
            <option value="">Select connection</option>
            {zen.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label || c.kind}
              </option>
            ))}
          </select>
        </label>
        <label>
          Model
          <select
            name="model"
            required
            key={models[0]?.id}
            defaultValue={models[0]?.id || ""}
          >
            <option value="">Select usable catalog model</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
                {m.free ? " · Free (preferred)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          Modal
          <select name="modal" required>
            <option value="">Select Modal connection</option>
            {modal.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label || c.kind}
              </option>
            ))}
          </select>
        </label>
        <label>
          GitHub
          <select name="github" required>
            <option value="">Select GitHub connection</option>
            {github.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label || c.kind}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        What should the agent do?
        <textarea name="prompt" required rows={4} />
      </label>
      <button disabled={!models.length || !modal.length || !github.length}>
        Create Session and queue request
      </button>
      <small>
        Models come from the selected connection's recent validation. No Codex
        connection is needed.
      </small>
    </Form>
  );
}

function Connections({
  wid,
  items,
  reload,
}: {
  wid: string;
  items: Connection[];
  reload: () => Promise<void>;
}) {
  const [kind, setKind] = useState<Connection["kind"]>("opencode_zen");
  return (
    <>
      <Form
        onSubmit={async (data) => {
          const credential =
            kind === "modal"
              ? {
                  token_id: value(data, "token_id"),
                  token_secret: value(data, "token_secret"),
                }
              : kind === "github"
                ? { token: value(data, "token") }
                : { api_key: value(data, "api_key") };
          await api.request(`/api/workspaces/${wid}/connections`, "POST", {
            kind,
            label: value(data, "label"),
            credential,
          });
          await reload();
        }}
      >
        <h2>Add manual connection</h2>
        <label>
          Provider
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as Connection["kind"])}
          >
            <option value="opencode_zen">OpenCode Zen</option>
            <option value="modal">Modal</option>
            <option value="github">GitHub</option>
          </select>
        </label>
        <label>
          Label
          <input name="label" />
        </label>
        {(kind === "modal"
          ? ["token_id", "token_secret"]
          : kind === "github"
            ? ["token"]
            : ["api_key"]
        ).map((name) => (
          <label key={name}>
            {name.replaceAll("_", " ")}
            <input name={name} type="password" autoComplete="off" required />
          </label>
        ))}
        <button>Store encrypted and validate</button>
      </Form>
      <section>
        <h2>Your connections</h2>
        {items.map((c) => (
          <article key={c.id}>
            <h3>{c.label || c.kind}</h3>
            <p>
              {c.state} · {c.health} · version {c.version}
            </p>
            <Form
              onSubmit={async () => {
                await api.request(
                  `/api/connections/${c.id}/validations`,
                  "POST",
                  { expected_version: c.version },
                );
                await reload();
              }}
            >
              <button disabled={c.state !== "configured"}>
                Validate connection
              </button>
            </Form>
            <details>
              <summary>Replace credential</summary>
              <Form
                onSubmit={async (data) => {
                  const credential =
                    c.kind === "modal"
                      ? {
                          token_id: value(data, "token_id"),
                          token_secret: value(data, "token_secret"),
                        }
                      : c.kind === "github"
                        ? { token: value(data, "token") }
                        : { api_key: value(data, "api_key") };
                  await api.request(
                    `/api/connections/${c.id}/credential-versions`,
                    "POST",
                    { expected_version: c.version, credential },
                  );
                  await reload();
                }}
              >
                {(c.kind === "modal"
                  ? ["token_id", "token_secret"]
                  : c.kind === "github"
                    ? ["token"]
                    : ["api_key"]
                ).map((name) => (
                  <label key={name}>
                    {name.replaceAll("_", " ")}
                    <input
                      name={name}
                      type="password"
                      autoComplete="off"
                      required
                    />
                  </label>
                ))}
                <button>Replace with new version</button>
              </Form>
            </details>
            <Form
              onSubmit={async () => {
                await api.request(`/api/connections/${c.id}`, "DELETE", {
                  expected_version: c.version,
                });
                await reload();
              }}
            >
              <button className="danger" disabled={c.state === "revoked"}>
                Disconnect
              </button>
            </Form>
          </article>
        ))}
      </section>
    </>
  );
}

function Settings({ user }: { user: User }) {
  return (
    <section>
      <h2>Account</h2>
      <p>
        {user.email} · {user.verified_at ? "Verified" : "Verification required"}
      </p>
      <Form
        onSubmit={async (data) => {
          await api.request("/api/auth/password-changes", "POST", {
            current_password: value(data, "current_password"),
            password: value(data, "password"),
          });
          cache.clear();
          location.reload();
        }}
      >
        <label>
          Current password
          <input
            name="current_password"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        <label>
          New password
          <input
            name="password"
            type="password"
            minLength={8}
            autoComplete="new-password"
            required
          />
        </label>
        <button>Change password and sign out</button>
      </Form>
      <label>
        Theme
        <select
          defaultValue={document.documentElement.dataset.theme || "dark"}
          onChange={(e) => {
            document.documentElement.dataset.theme = e.target.value;
            localStorage.setItem("sbx.theme", e.target.value);
          }}
        >
          <option value="dark">Dark</option>
          <option value="light">Light</option>
        </select>
      </label>
      <p>
        Credentials and Session state stay on the server. Local storage contains
        only the theme preference.
      </p>
    </section>
  );
}

function Detail({ id, open }: { id: string; open: (id: string) => void }) {
  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [tab, setTab] = useState("Conversation");
  const [changes, setChanges] = useState<Changeset[]>([]);
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const [delegation, setDelegation] = useState<Delegation | null>(null);
  const [error, setError] = useState("");
  const [files, setFiles] = useState<{ path: string; size: number }[]>([]);
  const [file, setFile] = useState<{
    path: string;
    content: string;
    digest: string;
  } | null>(null);
  const [terminal, setTerminal] = useState("");
  const [output, setOutput] = useState("");
  const refresh = useCallback(async () => {
    try {
      const s = await api.session(id);
      const [e, c, replay] = await Promise.all([
        api.events(id, Math.max(0, s.event_watermark - 100)),
        api.changesets(id),
        api.events(id, s.event_watermark),
      ]);
      let state = initial(s);
      for (const event of replay.events) state = apply(state, event);
      if (state.resetRequired) state = initial(await api.session(id));
      cache.set(id, state);
      setSession(state.snapshot);
      setEvents(
        [...e.events, ...replay.events]
          .filter(
            (event, index, all) =>
              all.findIndex((e) => e.seq === event.seq) === index,
          )
          .slice(-200),
      );
      setChanges(c.items);
    } catch (e) {
      setError(String(e));
    }
  }, [id]);
  useEffect(() => {
    setSession(null);
    setTerminal("");
    setOutput("");
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (!terminal) return;
    const timer = setInterval(
      () =>
        void api
          .request<{ text: string }>(
            `/api/sessions/${id}/terminals/${terminal}`,
          )
          .then((r) => setOutput(r.text))
          .catch((e) => setError(String(e))),
      1000,
    );
    return () => clearInterval(timer);
  }, [terminal, id]);
  async function act(path: string, body: unknown = {}) {
    try {
      const result = await api.request<Record<string, string>>(
        path,
        "POST",
        body,
      );
      await refresh();
      return result;
    } catch (e) {
      setError(String(e));
      return null;
    }
  }
  if (!session) return <p role="status">Loading Session… {error}</p>;
  const s = session;
  const ready = changes.filter((c) => c.state === "ready");
  return (
    <>
      <section className="session-heading">
        <h2>{s.title}</h2>
        <p>
          {s.lifecycle} · Worktree {s.worktree.availability} · generation{" "}
          {s.worktree.generation}
        </p>
        <small>Saved checkpoint: {s.worktree.last_snapshot_id || "none"}</small>
        <div className="actions">
          <button
            onClick={() =>
              void act(`/api/sessions/${id}/snapshots`, {
                generation: s.worktree.generation,
              })
            }
          >
            Save checkpoint
          </button>
          <button
            onClick={() => void act(`/api/sessions/${id}/executor/releases`)}
          >
            Release compute
          </button>
          <button
            onClick={() =>
              void act(`/api/sessions/${id}/archives`, {
                expected_version: s.version,
              })
            }
          >
            Archive
          </button>
          <button
            onClick={() =>
              void act(`/api/sessions/${id}/closures`, {
                expected_version: s.version,
              })
            }
          >
            Close
          </button>
        </div>
      </section>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <nav className="tabs" aria-label="Session panels">
        {[
          "Conversation",
          "Activity",
          "Changes",
          "Files",
          "Terminal",
          "Services / Preview",
          "Child Sessions",
        ].map((t) => (
          <button
            key={t}
            aria-current={tab === t ? "page" : undefined}
            onClick={() => {
              setTab(t);
              if (t === "Files")
                void api
                  .request<{ path: string; size: number }[]>(
                    `/api/sessions/${id}/files`,
                  )
                  .then(setFiles)
                  .catch((e) => setError(String(e)));
            }}
          >
            {t}
          </button>
        ))}
      </nav>
      {tab === "Conversation" ? (
        <>
          <section className="conversation">
            {s.messages.map((m) => (
              <article key={m.id} className="message">
                <strong>You</strong>
                <p>{m.content}</p>
              </article>
            ))}
            {s.turns.map((t) => (
              <article key={t.id}>
                <strong>
                  Turn {t.ordinal} · {t.state}
                </strong>
                <pre>
                  {t.outcome?.text ||
                    s.parts
                      .filter((p) => p.kind === "text")
                      .map((p) => p.content)
                      .join("\n")}
                </pre>
                {!t.evidence_complete && (
                  <small>Evidence incomplete or Turn pending</small>
                )}
                {["queued", "preparing", "running", "cancelling"].includes(
                  t.state,
                ) && (
                  <button
                    onClick={() => void act(`/api/turns/${t.id}/cancellations`)}
                  >
                    Cancel Turn
                  </button>
                )}
              </article>
            ))}
          </section>
          <Form
            onSubmit={async (data) => {
              await api.request(`/api/sessions/${id}/messages`, "POST", {
                content: value(data, "content"),
                routing: "queue",
              });
              await refresh();
            }}
          >
            <label>
              Follow-up
              <textarea name="content" required />
            </label>
            <button disabled={s.lifecycle !== "open"}>Queue follow-up</button>
          </Form>
        </>
      ) : tab === "Activity" ? (
        <section>
          {events.map((e) => (
            <article key={e.id}>
              <span className="badge">#{e.seq}</span> <strong>{e.type}</strong>
              <pre>{JSON.stringify(e.payload, null, 2)}</pre>
            </article>
          ))}
        </section>
      ) : tab === "Changes" ? (
        <section>
          <button
            onClick={() =>
              void act(`/api/sessions/${id}/changesets`, {
                generation: s.worktree.generation,
                origin: "explicit",
                source_turn_id: s.turns.at(-1)?.id,
              })
            }
          >
            Capture immutable ChangeSet
          </button>
          {changes.map((c) => (
            <article key={c.id}>
              <h3>{c.id}</h3>
              <p>
                {c.state} · {c.subject_digest}
              </p>
              {c.manifest?.subject.files.map((f) => (
                <p key={f.path}>
                  {f.type} {f.path}
                </p>
              ))}
              <button
                disabled={c.state !== "ready"}
                onClick={() =>
                  void act(`/api/changesets/${c.id}/deliveries`, {
                    transport: "pull_request",
                  }).then((r) => {
                    if (r) void api.delivery(r.delivery_id).then(setDelivery);
                  })
                }
              >
                Deliver draft PR
              </button>
              <button
                disabled={c.state !== "ready"}
                onClick={() =>
                  void act(`/api/sessions/${id}/delegations`, {
                    changeset_id: c.id,
                    role: "review",
                  }).then((r) => {
                    if (r)
                      void api.delegation(r.delegation_id).then(setDelegation);
                  })
                }
              >
                Spawn independent review
              </button>
            </article>
          ))}
          {delivery && (
            <article>
              <h3>Delivery · {delivery.state}</h3>
              <p>{delivery.reason}</p>
              {delivery.pr_url && (
                <a href={delivery.pr_url} target="_blank" rel="noreferrer">
                  Open draft PR
                </a>
              )}
              <button
                onClick={() => void api.delivery(delivery.id).then(setDelivery)}
              >
                Refresh projection
              </button>
              <button
                onClick={() =>
                  void act(`/api/deliveries/${delivery.id}/refreshes`)
                }
              >
                Reconcile remote
              </button>
              <button
                onClick={() =>
                  void act(`/api/deliveries/${delivery.id}/merge-requests`, {
                    expected_version: delivery.version,
                    subject_digest: delivery.subject_digest,
                    expected_head: delivery.mapped_head,
                    method: "squash",
                  })
                }
              >
                Request gated merge
              </button>
              <pre>{JSON.stringify(delivery.steps, null, 2)}</pre>
            </article>
          )}
        </section>
      ) : tab === "Files" ? (
        <section>
          <div className="file-layout">
            <ul>
              {files.map((f) => (
                <li key={f.path}>
                  <button
                    onClick={() =>
                      void api
                        .request<{
                          path: string;
                          content: string;
                          digest: string;
                        }>(`/api/sessions/${id}/files?path=${encodeURIComponent(f.path)}`)
                        .then(setFile)
                    }
                  >
                    {f.path}
                  </button>
                </li>
              ))}
            </ul>
            {file && (
              <Form
                key={file.path + file.digest}
                onSubmit={async (data) => {
                  await api.request(`/api/sessions/${id}/files`, "PUT", {
                    path: file.path,
                    content: value(data, "content"),
                    digest: file.digest,
                    generation: s.worktree.generation,
                  });
                  await refresh();
                }}
              >
                <label>
                  {file.path}
                  <textarea
                    className="editor"
                    name="content"
                    defaultValue={file.content}
                    rows={20}
                  />
                </label>
                <button>Save with digest and generation</button>
              </Form>
            )}
          </div>
        </section>
      ) : tab === "Terminal" ? (
        <section>
          <p>
            Terminal is lease-local. An open writer blocks coding and captures
            until it is closed.
          </p>
          <button
            disabled={!!terminal}
            onClick={() =>
              void act(`/api/sessions/${id}/terminals`, {}).then((r) => {
                if (r) setTerminal(r.operation_id);
              })
            }
          >
            Open isolated terminal
          </button>
          {terminal && (
            <>
              <pre role="log">{output}</pre>
              <Form
                onSubmit={async (data) => {
                  await api.request(
                    `/api/sessions/${id}/terminals/${terminal}/inputs`,
                    "POST",
                    { content: value(data, "command") + "\n" },
                  );
                }}
              >
                <label>
                  Command
                  <input name="command" required />
                </label>
                <button>Send</button>
              </Form>
              <button
                onClick={() =>
                  void act(
                    `/api/sessions/${id}/terminals/${terminal}/closures`,
                  ).then(() => setTerminal(""))
                }
              >
                Close terminal
              </button>
            </>
          )}
        </section>
      ) : tab === "Child Sessions" ? (
        <section>
          {ready.map((c) => (
            <Form
              key={c.id}
              onSubmit={async (data) => {
                const r = await api.request<{ delegation_id: string }>(
                  `/api/sessions/${id}/delegations`,
                  "POST",
                  {
                    changeset_id: c.id,
                    role: value(data, "role"),
                    summary: value(data, "summary"),
                  },
                );
                setDelegation(await api.delegation(r.delegation_id));
              }}
            >
              <h3>Pinned subject {c.id}</h3>
              <label>
                Role
                <select name="role">
                  <option>review</option>
                  <option>test</option>
                  <option>research</option>
                  <option>integration</option>
                </select>
              </label>
              <label>
                Assignment
                <textarea name="summary" />
              </label>
              <button>Spawn child</button>
            </Form>
          ))}
          {delegation && (
            <article>
              <h3>
                {delegation.role} · {delegation.state}
              </h3>
              <button onClick={() => open(delegation.child_session_id)}>
                Open child Session
              </button>
              <button
                onClick={() =>
                  void api.delegation(delegation.id).then(setDelegation)
                }
              >
                Refresh result
              </button>
              <button
                onClick={() =>
                  void act(`/api/delegations/${delegation.id}/waits`, {
                    seconds: 600,
                  })
                }
              >
                Subscribe to result
              </button>
              <button
                onClick={() =>
                  void act(`/api/delegations/${delegation.id}/cancellations`)
                }
              >
                Cancel child
              </button>
              <pre>{JSON.stringify(delegation.result, null, 2)}</pre>
            </article>
          )}
        </section>
      ) : (
        <Services sid={id} />
      )}
    </>
  );
}

function Services({ sid }: { sid: string }) {
  const [items, setItems] = useState<
    {
      id: string;
      name: string;
      desired_state: string;
      instance?: { state: string };
    }[]
  >([]);
  const [error, setError] = useState("");
  const load = useCallback(
    () =>
      api
        .request<{ items: typeof items }>(`/api/sessions/${sid}/services`)
        .then((r) => setItems(r.items))
        .catch((e) => setError(String(e))),
    [sid],
  );
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <section>
      <h2>Declared services</h2>
      <p>
        Services use the ProjectVersion declaration and current lease. Preview
        opens on a separate origin.
      </p>
      {error && <p role="alert">{error}</p>}
      {!items.length && <p>No services declared in this ProjectVersion.</p>}
      {items.map((i) => (
        <article key={i.id}>
          <h3>{i.name}</h3>
          <p>
            {i.desired_state} · {i.instance?.state || "not realized"}
          </p>
          {["activations", "stops"].map((action) => (
            <button
              key={action}
              onClick={() =>
                void api
                  .request(`/api/services/${i.id}/${action}`, "POST", {})
                  .then(load)
                  .catch((e) => setError(String(e)))
              }
            >
              {action === "activations" ? "Start service" : "Stop service"}
            </button>
          ))}
          <button
            onClick={() =>
              void api
                .request<{ url: string }>(
                  `/api/services/${i.id}/preview-grants`,
                  "POST",
                  {},
                )
                .then((r) =>
                  window.open(r.url, "_blank", "noopener,noreferrer"),
                )
                .catch((e) => setError(String(e)))
            }
          >
            Open preview
          </button>
        </article>
      ))}
    </section>
  );
}
