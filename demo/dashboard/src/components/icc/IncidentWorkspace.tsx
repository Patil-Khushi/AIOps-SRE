import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, RefreshCw, Sparkles } from 'lucide-react';
import type { IncidentRowVM } from '@/lib/incidentVm';
import { useIncidentInvestigation } from '@/hooks/useIncidentInvestigation';
import { deriveLifecycle } from '@/lib/lifecycle';
import type { Phase } from '@/hooks/useHitlApply';
import { isActionable } from '@/lib/rcaDerive';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { LifecycleBar } from './LifecycleBar';
import { RootCauseHero } from './RootCauseHero';
import { WorkspaceTabs, type TabId } from './WorkspaceTabs';
import { RemediationPanel } from './RemediationPanel';
import ErrorBoundary from '@/components/ErrorBoundary';
import { api } from '@/lib/api';
import { useToast } from '@/hooks/useToast';
import { useChatDock } from '@/components/chat/ChatDockProvider';

const VERIFY_POLL_MS = 5000;

// The expanded view — reached via /console/incidents/:incidentId. Everything
// here reads from ONE POST /api/rca response (useIncidentInvestigation): the
// 7 tabs are a rendering concern over investigation, not a fetch-per-tab
// model, because the backend already returns the whole Investigation tree in
// one call.
export function IncidentWorkspace({
  row,
  onBack,
  onResolved,
}: {
  row: IncidentRowVM;
  onBack: () => void;
  onResolved?: () => void;
}) {
  const { verdict, investigation, loading, error, run } = useIncidentInvestigation(row);
  const [tab, setTab] = useState<TabId>('hypotheses');
  // Fed by RemediationPanel's single useHitlApply instance (via onPhaseHint)
  // rather than a second hook instance here — two independent useHitlApply
  // calls for the same verdict would keep two copies of apply/approve state
  // that could disagree with each other.
  const [hitlPhase, setHitlPhase] = useState<Phase | undefined>(undefined);
  // Set once the resolution_verifier's polled status actually reports 'pass'
  // — without this, "Verifying" and "Resolved" were structurally unreachable
  // on this page: deriveLifecycle only advances past "Remediating" when told
  // to, and nothing here ever told it to.
  const [resolved, setResolved] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const toast = useToast();
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const dock = useChatDock();

  // Scope the floating RCA agent launcher to this incident so it's available
  // from the workspace itself, not only after clicking "Debug" from the list.
  // Keyed on dock.row's id too (not just row.id) so this self-corrects on the
  // very first mount: if dock.row was still null the instant this effect
  // first ran, the mismatch re-fires the effect and applies focus() again,
  // instead of the launcher staying hidden until some later, unrelated
  // dock-state change happens to trigger a re-render. focus() itself is a
  // no-op once the ids already match, so this can't loop.
  useEffect(() => {
    if (dock.row?.id !== row.id) dock.focus(row);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.id, dock.row?.id]);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };
  useEffect(() => stopPolling, []);

  const handleResolved = () => {
    const incidentId = row.incidentId;
    if (!incidentId) {
      // No ServiceNow ticket to key a verification run by (e.g. a Suppressed
      // duplicate triage never opened one) — the backend's _post_fix_verify
      // never fires in that case, so polling would wait forever. Fall back
      // to the pre-verification behavior: the flag flip itself is resolution.
      setResolved(true);
      onResolved?.();
      return;
    }
    setVerifying(true);
    const poll = async () => {
      try {
        const res = await api.rcaVerifyStatus(incidentId);
        if (res.status === 'in_progress' || res.status === 'not_triggered') return; // keep polling
        stopPolling();
        setVerifying(false);
        if (res.status === 'pass') {
          setResolved(true);
          onResolved?.();
        } else {
          toast.push(
            `Verification did not pass (${res.status}) — the fix was applied but symptoms may persist.`,
            'bad',
          );
        }
      } catch {
        // A transient poll failure isn't a verification failure — keep trying.
      }
    };
    void poll();
    pollRef.current = setInterval(poll, VERIFY_POLL_MS);
  };

  const lifecycle = deriveLifecycle({
    hasVerdict: true,
    rcaBusy: loading,
    hasRcaVerdict: !!verdict,
    rcaActionable: verdict ? isActionable(verdict.root_cause_status) : undefined,
    hitlPhase,
    verifying,
    resolved,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-[var(--icc-fg-muted)] transition hover:text-[var(--icc-fg)]"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back to incident list
        </button>
        <button
          type="button"
          onClick={() => {
            // A stale "Resolved"/"Remediating"/"Verifying" from the previous
            // verdict must not linger across a fresh investigation.
            stopPolling();
            setVerifying(false);
            setResolved(false);
            setHitlPhase(undefined);
            run();
          }}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-md border border-[var(--icc-accent)]/40 bg-[var(--icc-accent-soft)] px-2.5 py-1 text-xs font-medium text-[var(--icc-accent)] transition hover:bg-[var(--icc-accent)]/20 disabled:opacity-50"
        >
          {loading ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          {verdict ? 'Re-run investigation' : 'Run investigation'}
        </button>
      </div>

      <div className="rounded-xl border border-[var(--icc-border)] bg-[var(--icc-surface)] p-4">
        <LifecycleBar stage={lifecycle.stage} reached={lifecycle.reached} unknown={lifecycle.unknown} />
      </div>

      {!verdict && !loading && !error && (
        <div className="rounded-xl border border-dashed border-[var(--icc-border-strong)] p-8 text-center">
          <EmptyState
            label={`Analyse ${row.service}`}
            hint="Run the RCA investigation to produce a root cause, evidence, blast radius, and an approvable remediation."
          />
        </div>
      )}
      {loading && !verdict && (
        <div className="rounded-xl border border-[var(--icc-border)] p-8">
          <LoadingState label="Assembling the context pack, scoring hypotheses…" />
        </div>
      )}
      {error && <ErrorState error={error} />}

      {verdict && (
        <>
          <ErrorBoundary label="RootCauseHero">
            <RootCauseHero verdict={verdict} investigation={investigation} />
          </ErrorBoundary>

          <div className="rounded-xl border border-[var(--icc-border)] bg-[var(--icc-surface)] p-4">
            <WorkspaceTabs investigation={investigation} verdict={verdict} active={tab} onChange={setTab} />
          </div>

          <div className="rounded-xl border border-[var(--icc-border)] bg-[var(--icc-surface)] p-4">
            <p className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg-muted)]">
              Remediation
            </p>
            <ErrorBoundary label="RemediationPanel">
              <RemediationPanel
                verdict={verdict}
                incidentId={row.incidentId}
                investigation={investigation}
                onResolved={handleResolved}
                onPhaseHint={setHitlPhase}
              />
            </ErrorBoundary>
          </div>
        </>
      )}
    </div>
  );
}
