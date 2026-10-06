import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { useQueryClient } from "./context";

interface Entry {
  data?: unknown;
  error?: unknown;
  loading: boolean;
  updatedAt: number;
  promise?: Promise<void>;
}

/**
 * In-memory, bounded (LRU) query cache scoped by an opaque scope string
 * (user/workspace). Never persisted; `clear()` runs on logout.
 */
export class QueryClient {
  private entries = new Map<string, Entry>();
  private listeners = new Set<() => void>();
  private version = 0;
  constructor(
    readonly scope: string,
    private readonly maxEntries = 200,
  ) {}

  private k(key: readonly unknown[]) {
    return `${this.scope}|${key.join("/")}`;
  }
  private emit() {
    this.version++;
    this.listeners.forEach((l) => l());
  }
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  getVersion = () => this.version;

  get(key: readonly unknown[]): Entry | undefined {
    return this.entries.get(this.k(key));
  }

  set(key: readonly unknown[], data: unknown) {
    this.put(this.k(key), { data, loading: false, updatedAt: Date.now() });
    this.emit();
  }

  private put(k: string, entry: Entry) {
    this.entries.delete(k); // refresh LRU position
    this.entries.set(k, entry);
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value as string;
      this.entries.delete(oldest);
    }
  }

  fetch<T>(key: readonly unknown[], fn: () => Promise<T>): Promise<void> {
    const k = this.k(key);
    const prev = this.entries.get(k);
    if (prev?.promise) return prev.promise;
    const promise = fn().then(
      (data) => {
        this.put(k, { data, loading: false, updatedAt: Date.now() });
        this.emit();
      },
      (error) => {
        const cur = this.entries.get(k);
        this.put(k, { data: cur?.data, error, loading: false, updatedAt: Date.now() });
        this.emit();
      },
    );
    this.put(k, { ...(prev ?? { updatedAt: 0 }), error: undefined, loading: true, promise });
    this.emit();
    return promise;
  }

  /** Drop entries whose key starts with `prefix` (stale data is refetched by active hooks). */
  invalidate(prefix: readonly unknown[]) {
    const p = this.k(prefix);
    for (const k of [...this.entries.keys()]) {
      if (k === p || k.startsWith(`${p}/`)) this.entries.delete(k);
    }
    this.emit();
  }

  clear() {
    this.entries.clear();
    this.emit();
  }
  get size() {
    return this.entries.size;
  }
}

export interface QueryResult<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  refetch: () => void;
}

/** `key === null` disables the query. */
export function useQuery<T>(
  key: readonly unknown[] | null,
  fn: () => Promise<T>,
  opts: { pollMs?: number } = {},
): QueryResult<T> {
  const qc = useQueryClient();
  useSyncExternalStore(qc.subscribe, qc.getVersion);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const keyStr = key ? key.join("/") : null;
  const keyRef = useRef(key);
  keyRef.current = key;

  const entry = key ? qc.get(key) : undefined;
  const missing = key !== null && entry === undefined;
  useEffect(() => {
    if (keyRef.current && missing) void qc.fetch(keyRef.current, () => fnRef.current());
  }, [qc, keyStr, missing]);

  useEffect(() => {
    if (!opts.pollMs || !keyRef.current) return;
    const id = setInterval(() => {
      if (keyRef.current) void qc.fetch(keyRef.current, () => fnRef.current());
    }, opts.pollMs);
    return () => clearInterval(id);
  }, [qc, keyStr, opts.pollMs]);

  const refetch = useCallback(() => {
    if (keyRef.current) void qc.fetch(keyRef.current, () => fnRef.current());
  }, [qc]);

  return {
    data: entry?.data as T | undefined,
    error: entry?.error,
    loading: key !== null && (missing || (entry?.loading ?? false)),
    refetch,
  };
}
