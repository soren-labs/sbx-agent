import { useCallback, useRef, useState } from "react";
import { randomKey } from "../api/http";
import { isApiError } from "../api/errors";

/**
 * Runs a mutation with a stable Idempotency-Key: the key is reused only while the
 * previous attempt's outcome is unknown, and renewed after any definite result.
 */
export function useAction<A extends unknown[], R>(fn: (key: string, ...args: A) => Promise<R>) {
  const keyRef = useRef<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    const key = (keyRef.current ??= randomKey());
    setPending(true);
    setError(null);
    try {
      const r = await fnRef.current(key, ...args);
      keyRef.current = null;
      return r;
    } catch (e) {
      const unknown = isApiError(e) && (e.code === "outcome_unknown" || e.status === 0);
      if (!unknown) keyRef.current = null;
      setError(e);
      return undefined;
    } finally {
      setPending(false);
    }
  }, []);
  const reset = useCallback(() => {
    keyRef.current = null;
    setError(null);
  }, []);
  return { run, pending, error, reset };
}
