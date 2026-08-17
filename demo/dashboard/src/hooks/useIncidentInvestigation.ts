import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { makeCache } from '@/lib/persistentCache';
import { runIdFor } from '@/lib/runId';
import type { IncidentRowVM } from '@/lib/incidentVm';
import type { RCAVerdict } from '@/types/api';

// Same namespace RcaConsole.tsx already uses for its own cache, so an RCA run
// on either surface warms the other.
const rcaCache = makeCache<RCAVerdict>('rca');

function rcaKey(row: IncidentRowVM): string {
  return `${row.service}:${row.severity}:${row.triageVerdict.audit_metadata.created_at || row.id}`;
}

// Wraps POST /api/rca for one incident row. Shares its run_id with the chat
// dock (runIdFor(row.id)) so a manual re-run here and a chat turn on the same
// incident land in the same session server-side.
export function useIncidentInvestigation(row: IncidentRowVM | null) {
  const [verdict, setVerdict] = useState<RCAVerdict | null>(() => (row ? rcaCache.get(rcaKey(row)) ?? null : null));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rowRef = useRef(row);
  rowRef.current = row;

  useEffect(() => {
    setError(null);
    setVerdict(row ? rcaCache.get(rcaKey(row)) ?? null : null);
  }, [row?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = useCallback(async () => {
    const current = rowRef.current;
    if (!current) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.rca(current.triageVerdict, undefined, {
        runId: runIdFor(current.id),
        incidentId: current.incidentId,
      });
      rcaCache.set(rcaKey(current), result);
      setVerdict(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  return {
    verdict,
    investigation: verdict?.investigation ?? null,
    loading,
    error,
    run,
    analysisId: row ? runIdFor(row.id) : null,
  };
}
