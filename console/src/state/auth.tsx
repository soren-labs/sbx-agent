import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { isApiError } from "../api/errors";
import type { Me, WorkspaceRef } from "../api/types";
import { QueryClient } from "./query";
import { QueryCtx, useApi } from "./context";

type Status = "loading" | "anonymous" | "authenticated";

interface AuthCtx {
  status: Status;
  me: Me | null;
  workspace: WorkspaceRef | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  reload: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("AuthProvider missing");
  return c;
}

/**
 * Owns the session identity and a query cache scoped by user+workspace.
 * The cache is replaced (purged) on logout, access loss and identity change.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const workspace = me?.workspaces[0] ?? null;
  const scope = me ? `${me.user.id}:${workspace?.id ?? ""}` : "anon";
  const qc = useMemo(() => new QueryClient(scope), [scope]);

  useEffect(() => () => qc.clear(), [qc]);

  const purge = useCallback(() => {
    setMe(null);
    setStatus("anonymous");
  }, []);

  const reload = useCallback(async () => {
    try {
      const m = await api.auth.me();
      setMe(m);
      setStatus("authenticated");
    } catch (e) {
      if (isApiError(e) && e.status === 0) {
        setStatus("anonymous");
        return;
      }
      purge();
    }
  }, [api, purge]);

  useEffect(() => {
    api.setUnauthenticatedHandler(purge);
    void reload();
    return () => api.setUnauthenticatedHandler(undefined);
  }, [api, reload, purge]);

  const login = useCallback(
    async (email: string, password: string) => {
      const r = await api.auth.login(email, password);
      setMe({ user: r.user, workspaces: r.workspaces, auth: r.auth });
      setStatus("authenticated");
    },
    [api],
  );

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } catch {
      /* cookie may already be gone; purge locally regardless */
    }
    qc.clear();
    purge();
  }, [api, qc, purge]);

  const value = useMemo(
    () => ({ status, me, workspace, login, logout, reload }),
    [status, me, workspace, login, logout, reload],
  );
  return (
    <Ctx.Provider value={value}>
      <QueryCtx.Provider value={qc}>{children}</QueryCtx.Provider>
    </Ctx.Provider>
  );
}
