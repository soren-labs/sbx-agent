import { expect, test } from "@playwright/test";

test("mock_api smoke: create session and read it back", async ({ request }) => {
  const created = await request.post("/api/sessions", {
    data: { title: "e2e-smoke", model: "gpt-5" },
  });
  expect(created.status()).toBe(201);
  const body = await created.json();
  expect(body.session_id).toBeTruthy();

  const got = await request.get(`/api/sessions/${body.session_id}`);
  expect(got.status()).toBe(200);
  const session = await got.json();
  expect(session.id).toBe(body.session_id);
  expect(session.title).toBe("e2e-smoke");
  expect(["creating", "idle"]).toContain(session.status);
});
