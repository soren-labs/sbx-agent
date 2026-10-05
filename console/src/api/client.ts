import type {
  Connection,
  Page,
  Session,
  User,
  Project,
  Model,
  Changeset,
  Delivery,
  Delegation,
  Event,
} from "./types";

export class ApiError extends Error {
  constructor(
    public code: string,
    public status: number,
  ) {
    super(code.replaceAll("_", " "));
  }
}

function csrf() {
  const value = document.cookie
    .split("; ")
    .find((c) => c.startsWith("sbx_csrf="));
  return value ? decodeURIComponent(value.split("=")[1]) : "";
}

export class Client {
  async request<T>(
    path: string,
    method = "GET",
    body?: unknown,
    key: string = crypto.randomUUID(),
  ): Promise<T> {
    const options: RequestInit = {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(method === "GET"
          ? {}
          : { "Idempotency-Key": key, "X-CSRF-Token": csrf() }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    };
    let response: Response;
    try {
      response = await fetch(path, options);
    } catch {
      response = await fetch(path, options);
    }
    const result = await response.json();
    if (!response.ok)
      throw new ApiError(
        result.error?.code ?? "request_failed",
        response.status,
      );
    return result as T;
  }
  me = () => this.request<User>("/api/me");
  login = (email: string, password: string) =>
    this.request("/api/auth/login", "POST", { email, password });
  logout = () => this.request("/api/auth/logout", "POST", {});
  connections = (wid: string) =>
    this.request<Page<Connection>>(`/api/workspaces/${wid}/connections`);
  models = (cid: string) =>
    this.request<{ models: Model[] }>(
      `/api/models?connection_id=${encodeURIComponent(cid)}`,
    );
  projects = (wid: string) =>
    this.request<Page<Project>>(`/api/workspaces/${wid}/projects`);
  sessions = (wid: string) =>
    this.request<Page<Session>>(`/api/workspaces/${wid}/sessions`);
  session = (sid: string) => this.request<Session>(`/api/sessions/${sid}`);
  events = (sid: string, after = 0) =>
    this.request<{ events: Event[]; event_watermark: number }>(
      `/api/sessions/${sid}/events?after=${after}`,
    );
  changesets = (sid: string) =>
    this.request<Page<Changeset>>(`/api/sessions/${sid}/changesets`);
  delivery = (id: string) => this.request<Delivery>(`/api/deliveries/${id}`);
  delegation = (id: string) =>
    this.request<Delegation>(`/api/delegations/${id}`);
}
export const api = new Client();
