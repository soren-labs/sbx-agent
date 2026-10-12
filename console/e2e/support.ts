import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const API_ORIGIN = "http://127.0.0.1:8811";
export const PASSWORD = "correct horse battery 9";

export function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}@example.test`;
}

/** Verification link from the fake backend's test-only inbox. */
export async function verificationPath(request: APIRequestContext, email: string): Promise<string> {
  let body = "";
  await expect(async () => {
    const res = await request.get(`${API_ORIGIN}/__e2e/mail`, { params: { to: email } });
    expect(res.status()).toBe(200);
    body = (await res.json()).body;
  }).toPass();
  const url = new URL(body.slice(body.indexOf("http")).trim());
  return url.pathname + url.search;
}

export interface SignedIn {
  email: string;
  workspaceId: string;
  csrf: string;
}

/** Register, verify and sign in through the API; the page's context keeps the session cookie. */
export async function signInViaApi(page: Page, prefix: string): Promise<SignedIn> {
  const email = uniqueEmail(prefix);
  const request = page.request;
  const registered = await request.post("/api/auth/register", { data: { email, password: PASSWORD } });
  expect(registered.status()).toBe(202);
  const token = new URL(await verificationPath(request, email), "http://x").searchParams.get("token");
  const verified = await request.post("/api/auth/email-verifications", { data: { token } });
  expect(verified.status()).toBe(200);
  const login = await request.post("/api/auth/login", { data: { email, password: PASSWORD } });
  expect(login.status()).toBe(200);
  const me = await login.json();
  return { email, workspaceId: me.workspaces[0].id, csrf: me.csrf_token };
}

/** Add a BYOK inference Connection (validated by the fake connector) and wait until it is ready. */
export async function connectInference(page: Page, user: SignedIn): Promise<void> {
  const headers = { "X-CSRF-Token": user.csrf, "Idempotency-Key": crypto.randomUUID() };
  const created = await page.request.post(`/api/workspaces/${user.workspaceId}/connections`, {
    headers,
    data: {
      kind: "inference_api",
      label: "Fake inference",
      credential: {
        api_key: "REDACTED-browser-e2e-inference-key",
        base_url: "https://inference.example.test/v1",
        protocol: "openai_chat",
        model: "test-model",
      },
    },
  });
  expect(created.status()).toBe(201);
  const id = (await created.json()).id;
  await expect(async () => {
    const res = await page.request.get(`/api/connections/${id}`);
    expect((await res.json()).health).toBe("ready");
  }).toPass();
}
