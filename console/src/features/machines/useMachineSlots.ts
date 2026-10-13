import { useEffect } from "react";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";

/** Slots of the workspace, followed while anything is changing on the server. */
export function useMachineSlots() {
  const api = useApi();
  const qc = useQueryClient();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["machine-slots", w] : null, () => api.slots.list(w!));
  const moving = (q.data?.items ?? []).some((s) => s.status === "login_pending" || s.status === "deleting");
  const busy = (q.data?.items ?? []).some((s) => s.status === "running");
  useEffect(() => {
    if (!w || (!moving && !busy)) return;
    const id = setInterval(() => void qc.fetch(["machine-slots", w], () => api.slots.list(w)), moving ? 2000 : 6000);
    return () => clearInterval(id);
  }, [moving, busy, w, qc, api]);
  return q;
}
