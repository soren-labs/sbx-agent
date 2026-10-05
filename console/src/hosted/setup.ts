import { useCallback, useEffect, useState } from "react";
import { hostedRequest } from "./api";

export type SetupStatus = {
  loading: boolean;
  modal: boolean;
  github: boolean;
  opencode: boolean;
  done: number;
  refresh: () => void;
};

/** Readiness of the three hosted connections a Session needs. */
export function useSetupStatus(enabled = true): SetupStatus {
  const [state, setState] = useState({ loading: true, modal: false, github: false, opencode: false });
  const refresh = useCallback(() => {
    if (!enabled) return;
    void Promise.all([
      hostedRequest("/hosted/connections/modal").catch(() => null),
      hostedRequest("/hosted/connections/github").catch(() => null),
      hostedRequest("/hosted/connections/opencode").catch(() => null),
    ]).then(([modal, github, opencode]) =>
      setState({
        loading: false,
        modal: modal?.connection?.state === "ready",
        github: github?.connection?.state === "connected",
        opencode: opencode?.connection?.state === "connected",
      }),
    );
  }, [enabled]);
  useEffect(() => {
    refresh();
    window.addEventListener("sbx-connection-change", refresh);
    return () => window.removeEventListener("sbx-connection-change", refresh);
  }, [refresh]);
  const done = [state.modal, state.github, state.opencode].filter(Boolean).length;
  return { ...state, done, refresh };
}
