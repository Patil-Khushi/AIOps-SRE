import { lazy, Suspense } from 'react';
import type { Investigation } from '@/types/rca';
import type { RCAVerdict } from '@/types/api';
import { LoadingState } from '@/components/states';
import ErrorBoundary from '@/components/ErrorBoundary';

const HypothesesTab = lazy(() => import('./tabs/HypothesesTab').then((m) => ({ default: m.HypothesesTab })));
const EvidenceTab = lazy(() => import('./tabs/EvidenceTab').then((m) => ({ default: m.EvidenceTab })));
const TimelineTab = lazy(() => import('./tabs/TimelineTab').then((m) => ({ default: m.TimelineTab })));
const BlastRadiusTab = lazy(() => import('./tabs/BlastRadiusTab').then((m) => ({ default: m.BlastRadiusTab })));
const ChangesTab = lazy(() => import('./tabs/ChangesTab').then((m) => ({ default: m.ChangesTab })));
const HistoryTab = lazy(() => import('./tabs/HistoryTab').then((m) => ({ default: m.HistoryTab })));
const VerificationTab = lazy(() => import('./tabs/VerificationTab').then((m) => ({ default: m.VerificationTab })));

export type TabId = 'hypotheses' | 'evidence' | 'timeline' | 'blast_radius' | 'changes' | 'history' | 'verification';

const TABS: { id: TabId; label: string; countKey: (inv: Investigation | null) => number | null }[] = [
  { id: 'hypotheses', label: 'Hypotheses', countKey: (inv) => inv?.matrices.length ?? null },
  { id: 'evidence', label: 'Evidence', countKey: (inv) => (inv ? inv.matrices.reduce((n, m) => n + m.supporting.length + m.contradicting.length + m.checked_absent.length + m.gaps.length, 0) : null) },
  { id: 'timeline', label: 'Timeline', countKey: (inv) => inv?.timeline.events.length ?? null },
  { id: 'blast_radius', label: 'Blast radius', countKey: (inv) => inv?.blast_radius?.impacts.length ?? null },
  { id: 'changes', label: 'Changes', countKey: (inv) => (inv ? inv.timeline.events.filter((e) => e.is_change).length : null) },
  { id: 'history', label: 'History', countKey: (inv) => inv?.historical_influence.priors_applied.length ?? null },
  { id: 'verification', label: 'Verification', countKey: (inv) => (inv?.verification ? inv.verification.checks.length : null) },
];

// Only the active tab is mounted (not all seven behind CSS `hidden`) — each
// is React.lazy, so switching tabs pays a code-split cost once, not up front.
// Each also gets its own ErrorBoundary: a malformed section in one tab must
// not take the workspace down.
export function WorkspaceTabs({
  investigation,
  verdict,
  active,
  onChange,
}: {
  investigation: Investigation | null;
  verdict: RCAVerdict;
  active: TabId;
  onChange: (id: TabId) => void;
}) {
  return (
    <div>
      <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-[var(--icc-border)]">
        {TABS.map((t) => {
          const count = t.countKey(investigation);
          const isActive = t.id === active;
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={isActive}
              type="button"
              onClick={() => onChange(t.id)}
              className={
                'whitespace-nowrap border-b-2 px-3 py-2 text-xs font-medium transition-colors ' +
                (isActive
                  ? 'border-[var(--icc-accent)] text-[var(--icc-accent)]'
                  : 'border-transparent text-[var(--icc-fg-muted)] hover:text-[var(--icc-fg)]')
              }
            >
              {t.label} {count === null ? <span className="text-[var(--icc-fg-faint)]">· —</span> : `· ${count}`}
            </button>
          );
        })}
      </div>

      <div className="pt-4">
        <ErrorBoundary label={`Workspace · ${active}`}>
          <Suspense fallback={<LoadingState label="Loading…" />}>
            {active === 'hypotheses' && <HypothesesTab investigation={investigation} />}
            {active === 'evidence' && <EvidenceTab investigation={investigation} />}
            {active === 'timeline' && <TimelineTab investigation={investigation} />}
            {active === 'blast_radius' && <BlastRadiusTab investigation={investigation} />}
            {active === 'changes' && <ChangesTab investigation={investigation} service={verdict.affected_service} />}
            {active === 'history' && <HistoryTab investigation={investigation} />}
            {active === 'verification' && <VerificationTab investigation={investigation} />}
          </Suspense>
        </ErrorBoundary>
      </div>
    </div>
  );
}
