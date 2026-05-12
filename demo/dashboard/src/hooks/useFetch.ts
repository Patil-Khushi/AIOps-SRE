import { useEffect, useRef, useState, useCallback } from 'react';

interface State<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export function useFetch<T>(
  fetcher: () => Promise<T>,
  { intervalMs }: { intervalMs?: number } = {},
): State<T> & { refetch: () => Promise<void> } {
  const [state, setState] = useState<State<T>>({ data: null, loading: true, error: null });
  const aliveRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const run = useCallback(async () => {
    try {
      const data = await fetcherRef.current();
      if (!aliveRef.current) return;
      setState({ data, loading: false, error: null });
    } catch (e) {
      if (!aliveRef.current) return;
      const message = e instanceof Error ? e.message : String(e);
      setState((s) => ({ ...s, loading: false, error: message }));
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    run();
    let timer: ReturnType<typeof setInterval> | null = null;
    if (intervalMs && intervalMs > 0) {
      timer = setInterval(run, intervalMs);
    }
    return () => {
      aliveRef.current = false;
      if (timer) clearInterval(timer);
    };
  }, [intervalMs, run]);

  return { ...state, refetch: run };
}
