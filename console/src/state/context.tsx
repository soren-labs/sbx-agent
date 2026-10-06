import { createContext, useContext, useMemo, type ReactNode } from "react";
import { createApiClient, type ApiClient } from "../api/client";
import type { QueryClient } from "./query";

export const ApiCtx = createContext<ApiClient | null>(null);
export const QueryCtx = createContext<QueryClient | null>(null);

export function useApi(): ApiClient {
  const c = useContext(ApiCtx);
  if (!c) throw new Error("ApiProvider missing");
  return c;
}
export function useQueryClient(): QueryClient {
  const c = useContext(QueryCtx);
  if (!c) throw new Error("QueryProvider missing");
  return c;
}

/** One shared API client for the app (same-origin `/api`). */
export function ApiProvider({ client, children }: { client?: ApiClient; children: ReactNode }) {
  const value = useMemo(() => client ?? createApiClient(), [client]);
  return <ApiCtx.Provider value={value}>{children}</ApiCtx.Provider>;
}
