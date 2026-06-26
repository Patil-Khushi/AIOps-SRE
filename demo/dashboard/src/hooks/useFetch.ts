import { useEffect, useRef, useState, useCallback } from 'react';
import { makeCache, clearAllCaches } from '@/lib/persistentCache';

interface State<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

// Backed by localStorage so cached data survives page reloads and new tabs.
const fetchCache = makeCache<unknown>('fetch');

// Call after a scenario reset so pages don't show pre-reset data from cache.
// Clears EVERY dashboard cache namespace (fetch, triage, rca) in one shot —
// not just the fetch cache — which is the intended scenario-reset behaviour.
export function clearAllDashboardCaches(): void {
  clearAllCaches();
}

export function useFetch<T>(
  fetcher: () => Promise<T>,
  { intervalMs, cacheKey }: { intervalMs?: number; cacheKey?: string } = {},
): State<T> & { refetch: () => Promise<void> } {
  const cached = cacheKey !== undefined ? (fetchCache.get(cacheKey) as T | undefined) : undefined;

  const [state, setState] = useState<State<T>>(() =>
    cached !== undefined
      ? { data: cached, loading: false, error: null }
      : { data: null, loading: true, error: null },
  );

  const aliveRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const run = useCallback(async () => {
    try {
      const data = await fetcherRef.current();
      if (!aliveRef.current) return;
      if (cacheKey !== undefined) fetchCache.set(cacheKey, data);
      setState({ data, loading: false, error: null });
    } catch (e) {
      if (!aliveRef.current) return;
      const message = e instanceof Error ? e.message : String(e);
      setState((s) => ({ ...s, loading: false, error: message }));
    }
  }, [cacheKey]);

  useEffect(() => {
    aliveRef.current = true;

    const hasCached = cacheKey !== undefined && fetchCache.has(cacheKey);

    // Restore from cache immediately so there is no spinner on back-navigation.
    if (hasCached) {
      setState({ data: fetchCache.get(cacheKey) as T, loading: false, error: null });
    } else {
      setState({ data: null, loading: true, error: null });
    }

    // For endpoints with no polling interval, skip the background re-fetch when
    // the cache already has data. The "Refresh" / "Refresh verdicts" buttons call
    // refetch() to force a new agent run when the user explicitly wants it.
    const shouldFetch = !hasCached || (intervalMs !== undefined && intervalMs > 0);

    if (shouldFetch) run();

    let timer: ReturnType<typeof setInterval> | null = null;
    if (intervalMs && intervalMs > 0) {
      timer = setInterval(run, intervalMs);
    }
    return () => {
      aliveRef.current = false;
      if (timer) clearInterval(timer);
    };
  }, [intervalMs, run, cacheKey]);

  return { ...state, refetch: run };
}
