import { useEffect, useState } from 'react';
import type { Investigation } from '@/types/rca';
import type { ChangeContext } from '@/types/api';
import { SectionShell } from '@/components/icc/SectionShell';
import { changeEvents } from '@/lib/rcaDerive';
import { api } from '@/lib/api';
import { makeCache } from '@/lib/persistentCache';
import { formatClock } from '@/lib/format';

const changesCache = makeCache<ChangeContext>('icc-changes');

// A change is correlation, not cause — every row here is tagged "temporal
// correlation" and never presented as the root cause. The free view (timeline
// events flagged is_change) renders immediately; the richer ChangeContext
// (deploys/config/infra from api.correlate) is fetched once per service on
// first activation and degrades to the free view if it fails.
export function ChangesTab({ investigation, service }: { investigation: Investigation | null; service: string }) {
  const [context, setContext] = useState<ChangeContext | null>(changesCache.get(service) ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (changesCache.get(service)) return;
    let alive = true;
    setLoading(true);
    api
      .correlate(service)
      .then((res) => {
        if (!alive) return;
        if (res.deployment_context) {
          changesCache.set(service, res.deployment_context);
          setContext(res.deployment_context);
        }
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [service]);

  const freeEvents = investigation ? changeEvents(investigation) : [];

  return (
    <div className="space-y-3">
      {freeEvents.length === 0 && !context ? (
        <SectionShell state={loading ? 'loading' : 'empty'} message={loading ? 'Checking for recent changes…' : 'No changes found in the timeline'}>
          <div />
        </SectionShell>
      ) : (
        <ul className="space-y-1.5">
          {freeEvents.map((e, i) => (
            <li key={`free-${i}`} className="flex items-center gap-2 rounded-md border border-[var(--icc-warn)]/30 px-2.5 py-1.5 text-xs">
              <span className="rounded bg-[var(--icc-warn)]/15 px-1.5 py-0.5 text-[10px] font-medium text-[var(--icc-warn)]">
                temporal correlation
              </span>
              <span className="font-mono text-[var(--icc-fg-faint)]">{formatClock(e.timestamp)}</span>
              <span className="min-w-0 flex-1 truncate text-[var(--icc-fg)]">{e.service}: {e.event}</span>
            </li>
          ))}
          {context?.records.map((r) => (
            <li key={r.change_id} className="flex items-center gap-2 rounded-md border border-[var(--icc-border)] px-2.5 py-1.5 text-xs">
              <span className="rounded bg-[var(--icc-warn)]/15 px-1.5 py-0.5 text-[10px] font-medium text-[var(--icc-warn)]">
                temporal correlation
              </span>
              <span className="text-[10px] uppercase text-[var(--icc-fg-faint)]">{r.source}</span>
              <span className="min-w-0 flex-1 truncate text-[var(--icc-fg)]">{r.summary ?? r.change_type}</span>
              {r.url && (
                <a href={r.url} target="_blank" rel="noreferrer" className="text-[10px] text-[var(--icc-accent)]">
                  view
                </a>
              )}
            </li>
          ))}
        </ul>
      )}

      {context && context.sources_unavailable.length > 0 && (
        <p className="icc-dashed rounded-md border border-[var(--icc-unknown)]/40 px-3 py-2 text-[11px] text-[var(--icc-unknown)]">
          Change sources not examined: {context.sources_unavailable.join(', ')}
          {context.coverage_note ? ` — ${context.coverage_note}` : ''}
        </p>
      )}
      {error && !context && (
        <p className="text-[11px] text-[var(--icc-fg-faint)]">
          Could not fetch the richer change context ({error}) — showing the timeline-derived view only.
        </p>
      )}
    </div>
  );
}
