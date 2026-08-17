// Real-time RCA pipeline progress — GET /api/rca/stream/{run_id} (SSE).
//
// Copies useAlertsSocket()/useChatopsSocket()'s hook shape (lib/ws.ts) but for
// EventSource rather than raw WebSocket: EventSource gives auto-reconnect and
// Last-Event-ID resume natively, so there's no hand-rolled backoff here.
//
// The route emits named events (`event: received`, `event: evidence`, ...,
// `event: complete` / `event: failed`, plus `event: timeout` and `: ping`
// comment heartbeats) rather than the default `message` event, so each known
// RcaStage needs its own addEventListener — onmessage alone would never fire.

import { useEffect, useRef, useState } from 'react';
import type { RcaProgressEvent, RcaStage } from '@/types/rca';

const ALL_STAGES: RcaStage[] = [
  'received',
  'change_correlation',
  'evidence',
  'context_pack',
  'memory_recall',
  'action_vocabulary',
  'hypotheses',
  'explaining',
  'complete',
  'failed',
  'chat_turn_started',
  'chat_turn_answered',
];

export type RcaStreamStatus = 'idle' | 'connecting' | 'open' | 'done' | 'error';

export interface RcaProgressStageVM {
  stage: RcaStage;
  label: string;
  outcome: RcaProgressEvent['outcome'];
  seq: number;
}

export function useRcaProgress(analysisId: string | null): {
  stages: RcaProgressStageVM[];
  current: RcaProgressStageVM | null;
  status: RcaStreamStatus;
  done: boolean;
} {
  const [stages, setStages] = useState<RcaProgressStageVM[]>([]);
  const [status, setStatus] = useState<RcaStreamStatus>('idle');
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    setStages([]);
    if (!analysisId) {
      setStatus('idle');
      return;
    }
    setStatus('connecting');

    const es = new EventSource(`/api/rca/stream/${analysisId}`);
    esRef.current = es;

    const onStage = (ev: MessageEvent<string>) => {
      setStatus('open');
      try {
        const record = JSON.parse(ev.data) as RcaProgressEvent;
        setStages((prev) => {
          // A repeat stage_id (e.g. a "started" then a "done" for the same
          // stage) UPDATES in place rather than appending — keyed by stage,
          // matching the STARTED->OK/DEGRADED pairs agent.py emits.
          const next = [...prev];
          const idx = next.findIndex((s) => s.stage === record.stage);
          const vm: RcaProgressStageVM = { stage: record.stage, label: record.label, outcome: record.outcome, seq: record.seq };
          if (idx >= 0) next[idx] = vm;
          else next.push(vm);
          return next;
        });
        if (record.stage === 'complete' || record.stage === 'failed') {
          setStatus('done');
          es.close();
        }
      } catch {
        // malformed frame — ignore, keep streaming
      }
    };

    for (const stage of ALL_STAGES) es.addEventListener(stage, onStage);
    es.addEventListener('timeout', () => {
      setStatus('done');
      es.close();
    });
    es.onerror = () => {
      // EventSource retries on its own; if the run is genuinely gone the
      // server-side idle timeout closes it with `event: timeout` instead.
      setStatus((s) => (s === 'done' ? s : 'error'));
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [analysisId]);

  const current = stages.length > 0 ? stages[stages.length - 1] : null;
  return { stages, current, status, done: status === 'done' };
}
