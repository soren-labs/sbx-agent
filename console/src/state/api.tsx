import { createContext, useContext, type ReactNode } from "react";
import type { SessionApi } from "../api/client";
import { api as defaultApi } from "../api";

const Ctx = createContext<SessionApi>(defaultApi);

export function ApiProvider({
  client,
  children,
}: {
  client?: SessionApi;
  children: ReactNode;
}) {
  return <Ctx.Provider value={client ?? defaultApi}>{children}</Ctx.Provider>;
}

export function useApi(): SessionApi {
  return useContext(Ctx);
}
