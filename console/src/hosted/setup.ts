import { useCallback, useEffect, useState } from "react";
import { hostedRequest } from "./api";

export type SetupStatus = {
  loading: boolean;
  modal: boolean;
  github: boolean;
  codex: boolean;
  done: number;
  refresh: () => void;
};

/** Readiness of the three hosted connections a Session needs. */
export function useSetupStatus(enabled = true): SetupStatus {
  const [state, setState] = useState({ loading: true, modal: false, github: false, codex: false });
  const refresh = useCallback(() => {
    if (!enabled) return;
    void Promise.all([
      hostedRequest("/hosted/connections/modal").catch(() => null),
      hostedRequest("/hosted/connections/github").catch(() => null),
      hostedRequest("/hosted/connections/codex").catch(() => null),
    ]).then(([modal, github, codex]) =>
      setState({
        loading: false,
        modal: modal?.connection?.state === "ready",
        github: Boolean(github?.installations?.length),
        codex: ["connected", "refreshing"].includes(codex?.connection?.state),
      }),
    );
  }, [enabled]);
  useEffect(() => {
    refresh();
    window.addEventListener("sbx-connection-change", refresh);
    return () => window.removeEventListener("sbx-connection-change", refresh);
  }, [refresh]);
  const done = [state.modal, state.github, state.codex].filter(Boolean).length;
  return { ...state, done, refresh };
}
