// WebSocket helper with auto-reconnect and exponential backoff.
//
// The server pushes JSON frames of shape:
//   { type: 'alerts', alerts: PrometheusAlert[] }
//   { type: 'ping' }

import { useEffect, useRef, useState } from 'react';
import type { PrometheusAlert } from '@/types/api';

export interface AlertsFrame {
  type: 'alerts';
  alerts: PrometheusAlert[];
  fetched_at: string;
}

export type WSStatus = 'connecting' | 'open' | 'closed' | 'error';

function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  // In dev (Vite), proxy rewrites /ws → uvicorn. In prod, served same-origin.
  return `${proto}://${window.location.host}${path}`;
}

export function useAlertsSocket(): {
  alerts: PrometheusAlert[];
  status: WSStatus;
  lastUpdate: string | null;
} {
  const [alerts, setAlerts] = useState<PrometheusAlert[]>([]);
  const [status, setStatus] = useState<WSStatus>('connecting');
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const closedByUs = useRef(false);

  useEffect(() => {
    closedByUs.current = false;

    const connect = () => {
      setStatus('connecting');
      const ws = new WebSocket(wsUrl('/ws/alerts'));
      wsRef.current = ws;

      ws.onopen = () => {
        retryRef.current = 0;
        setStatus('open');
      };
      ws.onmessage = (ev) => {
        try {
          const frame = JSON.parse(ev.data) as AlertsFrame | { type: 'ping' };
          if (frame.type === 'alerts') {
            setAlerts(frame.alerts);
            setLastUpdate(frame.fetched_at);
          }
        } catch {
          // ignore malformed frame
        }
      };
      ws.onerror = () => setStatus('error');
      ws.onclose = () => {
        setStatus('closed');
        if (closedByUs.current) return;
        const delay = Math.min(1000 * 2 ** retryRef.current, 15_000);
        retryRef.current += 1;
        setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closedByUs.current = true;
      wsRef.current?.close();
    };
  }, []);

  return { alerts, status, lastUpdate };
}
