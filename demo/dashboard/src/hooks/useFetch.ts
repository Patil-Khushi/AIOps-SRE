import { useEffect, useRef, useState, useCallback } from 'react';

interface State<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

// Module-level cache: persists across React Router unmount/remount cycles so
// navigating back to a page shows the previous data immediately (no spinner)
// and revalidates quietly in the background.
const fetchCache = new Map<string, unknown>();

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
