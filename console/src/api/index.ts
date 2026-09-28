import { FixtureSessionApi } from "./mock";
import { HttpSessionApi } from "./http";
import type { SessionApi } from "./client";

/**
 * Select the API implementation. `VITE_API_MODE=http` (or a configured
 * VITE_API_BASE) uses the real control plane; anything else uses the typed
 * fixture client. Switching never touches UI code.
 */
export function createApi(): SessionApi {
  const mode = import.meta.env.VITE_API_MODE as string | undefined;
  const base = import.meta.env.VITE_API_BASE as string | undefined;
  if (mode === "http" || (mode !== "mock" && base)) return new HttpSessionApi();
  return new FixtureSessionApi();
}

export const api: SessionApi = createApi();

export { ApiError, isApiError } from "./client";
export type { SessionApi, SessionEventHandlers } from "./client";
export { FixtureSessionApi } from "./mock";
export { HttpSessionApi } from "./http";
export * from "./types";
