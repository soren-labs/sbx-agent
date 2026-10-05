import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { hostedRequest } from "../hosted/api";
import { useSetupStatus } from "../hosted/setup";

vi.mock("../hosted/api", () => ({ hostedRequest: vi.fn() }));
const request = vi.mocked(hostedRequest);
beforeEach(() => { request.mockReset(); });

it("counts manual GitHub and Zen as ready without Codex or an App installation", async () => {
  request.mockImplementation(async path => ({
    connection: { state: path.endsWith("modal") ? "ready" : "connected" },
    installations: [],
  }));
  const { result } = renderHook(() => useSetupStatus());
  await waitFor(() => expect(result.current.done).toBe(3));
  expect(result.current).toMatchObject({ modal: true, github: true, opencode: true });
  expect(request.mock.calls.map(([path]) => path)).toEqual([
    "/hosted/connections/modal", "/hosted/connections/github", "/hosted/connections/opencode",
  ]);
});

it.each(["invalid", "disabled"])("does not count %s manual connections, even with an App installation", async state => {
  request.mockResolvedValue({ connection: { state }, installations: [{ installation_id: 1 }] });
  const { result } = renderHook(() => useSetupStatus());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.done).toBe(0);
});

it("updates readiness after disconnect and fails closed when status cannot be loaded", async () => {
  request.mockImplementation(async path => ({ connection: { state: path.endsWith("modal") ? "ready" : "connected" } }));
  const { result } = renderHook(() => useSetupStatus());
  await waitFor(() => expect(result.current.done).toBe(3));
  request.mockRejectedValue(new Error("unavailable"));
  act(() => window.dispatchEvent(new Event("sbx-connection-change")));
  await waitFor(() => expect(result.current.done).toBe(0));
});

it("does not request hosted connection status outside hosted mode", () => {
  renderHook(() => useSetupStatus(false));
  expect(request).not.toHaveBeenCalled();
});
