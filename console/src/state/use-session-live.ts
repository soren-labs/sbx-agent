import { useEffect, useMemo, useSyncExternalStore } from "react";
import { useApi } from "./context";
import { SessionLive, type SessionLiveState } from "./session-live";

export function useSessionLive(sessionId: string): { live: SessionLive; state: SessionLiveState } {
  const api = useApi();
  const live = useMemo(() => new SessionLive(api, sessionId), [api, sessionId]);
  useEffect(() => {
    live.start();
    return () => live.stop();
  }, [live]);
  const state = useSyncExternalStore(live.subscribe, live.getState);
  return { live, state };
}
