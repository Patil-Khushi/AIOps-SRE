import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Siren } from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { useAlertsSocket } from '@/lib/ws';
import { makeCache } from '@/lib/persistentCache';
import { buildInvestigationPrompt, toIncidentRows, type IncidentRowVM } from '@/lib/incidentVm';
import { useChatDock } from '@/components/chat/ChatDockProvider';
import { IccRoot } from '@/components/icc/IccRoot';
import { ThemeSwitch } from '@/components/icc/ThemeSwitch';
import { IncidentKpiStrip } from '@/components/icc/IncidentKpiStrip';
import { IncidentToolbar } from '@/components/icc/IncidentToolbar';
import { IncidentTable } from '@/components/icc/IncidentTable';
import { IncidentWorkspace } from '@/components/icc/IncidentWorkspace';
import { LoadingState } from '@/components/states';
import type { Severity } from '@/types/api';

const orderCache = makeCache<string[]>('icc-order');

// ─── Incident Command Center (RCA Agent's operational surface) ─────────────
//
// The incident-list half of the ICC. The Debug button opens the RCA chat
// panel with the incident's investigation prompt auto-typed in (slice: chat
// panel) — for now it selects the row so the list is fully usable on its own.

export default function IncidentCommandCenter() {
  const dock = useChatDock();
  const navigate = useNavigate();
  const { incidentId } = useParams<{ incidentId?: string }>();
  // Poll only while showing the list — the workspace's own hook (POST
  // /api/rca) is the expensive one, and the list has no reason to keep
  // fetching while an incident's detail view is open.
  const verdicts = useFetch(() => api.verdicts({ limit: 200 }), {
    intervalMs: incidentId ? 0 : 8000,
    cacheKey: 'icc-verdicts',
  });
  const { alerts } = useAlertsSocket();
  const approvals = useFetch(() => api.approvals(false), {
    intervalMs: incidentId ? 0 : 5000,
    cacheKey: 'icc-approvals',
  });

  const rows = useMemo<IncidentRowVM[]>(
    () => toIncidentRows(verdicts.data?.verdicts ?? [], alerts),
    [verdicts.data, alerts],
  );

  // IMPORTANT: every hook below must run on EVERY render regardless of
  // `incidentId` — react-router reuses this same component instance across
  // /console/incidents and /console/incidents/:id (same route element), so an
  // early return before a hook call here changes the hook count between
  // renders and trips "Rendered fewer hooks than expected" (React error
  // #300/#310). The incidentId branch is handled in the JSX below instead.
  const [order, setOrder] = useState<string[]>(() => orderCache.get('order') ?? []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [severityFilter, setSeverityFilter] = useState<Set<Severity>>(new Set());

  // Reconcile persisted order with the current row set: keep known ids in
  // their saved position, append new ones, drop ids that no longer exist.
  useEffect(() => {
    const ids = new Set(rows.map((r) => r.id));
    const kept = order.filter((id) => ids.has(id));
    const added = rows.map((r) => r.id).filter((id) => !kept.includes(id));
    const next = [...kept, ...added];
    if (next.length !== order.length || next.some((id, i) => id !== order[i])) {
      setOrder(next);
      orderCache.set('order', next);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);

  const filtered = rows.filter((r) => {
    if (severityFilter.size > 0 && !severityFilter.has(r.severity)) return false;
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return r.service.toLowerCase().includes(q) || r.summary.toLowerCase().includes(q) || r.team.toLowerCase().includes(q);
  });

  const toggleSeverity = (s: Severity) =>
    setSeverityFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });

  const toggleCheck = (id: string) =>
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const handleReorder = (next: string[]) => {
    setOrder(next);
    orderCache.set('order', next);
  };

  const handleDebug = (row: IncidentRowVM) => {
    setSelectedId(row.id);
    dock.openFor(row, buildInvestigationPrompt(row));
  };

  if (incidentId) {
    const row = rows.find((r) => r.id === incidentId) ?? null;
    return (
      <IccRoot className="-m-6 min-h-[calc(100vh-4rem)] p-6">
        {!row && verdicts.loading ? (
          <LoadingState label="Loading incident…" />
        ) : !row ? (
          <div className="space-y-3">
            <button
              type="button"
              onClick={() => navigate('/console/incidents')}
              className="text-xs text-[var(--icc-fg-muted)] hover:text-[var(--icc-fg)]"
            >
              ← Back to incident list
            </button>
            <p className="text-sm text-[var(--icc-fg-muted)]">
              No incident found for this id — it may have resolved and dropped off the list.
            </p>
          </div>
        ) : (
          <IncidentWorkspace row={row} onBack={() => navigate('/console/incidents')} onResolved={verdicts.refetch} />
        )}
      </IccRoot>
    );
  }

  return (
    <IccRoot className="-m-6 min-h-[calc(100vh-4rem)] space-y-4 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-[var(--icc-fg)]">
            <Siren className="h-6 w-6 text-[var(--icc-accent)]" /> Incident Command Center
          </h1>
          <p className="mt-1 text-sm text-[var(--icc-fg-muted)]">
            Every triaged incident, live status, and one click into the RCA Agent.
          </p>
        </div>
        <ThemeSwitch />
      </div>

      <IncidentKpiStrip rows={rows} pendingApprovals={approvals.data?.count ?? null} />

      <IncidentToolbar
        query={query}
        onQuery={setQuery}
        severityFilter={severityFilter}
        onToggleSeverity={toggleSeverity}
        checkedCount={checkedIds.size}
        onBulkDebug={() => {
          filtered.filter((r) => checkedIds.has(r.id)).forEach(handleDebug);
        }}
        onRefresh={verdicts.refetch}
        refreshing={verdicts.loading}
      />

      <IncidentTable
        rows={filtered}
        order={order.filter((id) => filtered.some((r) => r.id === id))}
        onReorder={handleReorder}
        selectedId={selectedId}
        checkedIds={checkedIds}
        onSelect={setSelectedId}
        onToggleCheck={toggleCheck}
        onDebug={handleDebug}
        onOpenWorkspace={(row) => navigate(`/console/incidents/${row.id}`)}
        loading={verdicts.loading}
        error={verdicts.error}
      />
    </IccRoot>
  );
}
