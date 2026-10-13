import { useEffect } from "react";
import type { Connection } from "../../api/types";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";

const settling = (c: Connection) => c.state === "configured" && (c.health === "verifying" || c.health === "unverified");

/** Connections of the workspace, followed while server-side validation is still running. */
export function useConnections() {
  const api = useApi();
  const qc = useQueryClient();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["connections", w] : null, () => api.connections.list(w!));
  // Validation runs as a server Job: follow it until every Connection has a verdict.
  const waiting = (q.data?.items ?? []).some(settling);
  useEffect(() => {
    if (!waiting || !w) return;
    const id = setInterval(() => void qc.fetch(["connections", w], () => api.connections.list(w)), 2000);
    return () => clearInterval(id);
  }, [waiting, w, qc, api]);
  return q;
}
