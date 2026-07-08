import { useState, useEffect, useRef, type ReactNode } from 'react';
import {
  BookOpen,
  Sparkles,
  ShieldCheck,
  FileText,
  CheckCircle2,
  Clock,
  XCircle,
  ChevronRight,
  ExternalLink,
  Trash2,
  Calendar,
  Play,
  ListChecks,
  Loader2,
  Gavel,
} from 'lucide-react';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { clsx, timeAgo } from '@/lib/format';
import type { KBArticleRow, ReviewStatus, SynthesisResult } from '@/types/api';

// ─── canned resolved-incident bundles ──────────────────────────────────────
// In the full pipeline these come from Triage → RCA → fix-applied. For this
// demo segment we feed the synthesizer a resolved incident directly. The flag
// names and root causes are the real ones from demo/truth_files.

interface DemoScenario {
  key: string;
  label: string;
  service: string;
  bundle: () => Record<string, unknown>;
}

function bundle(service: string, incidentId: string, severity: string, summary: string, rootCause: string, flag: string) {
  const now = new Date();
  const iso = (minsAgo: number) => new Date(now.getTime() - minsAgo * 60_000).toISOString();
  return {
    incident_id: incidentId,
    resolved_at: now.toISOString(),
    triage_verdict: {
      affected_service: service,
      severity,
      alert_summary: summary,
      audit_metadata: { created_at: iso(10) },
    },
    rca_verdict: {
      affected_service: service,
      root_cause: rootCause,
      ranked_fix_steps: [
        {
          description: `Set flagd flag ${flag} to off via the feature-flags seam.`,
          blast_radius: 'low',
          rollback: 'Re-flip the flag back to on — instant.',
          action_type: 'set_flag',
          flag,
          variant: 'off',
        },
      ],
      confidence_score: 0.85,
      audit_metadata: { created_at: iso(5) },
    },
  };
}

const SCENARIOS: DemoScenario[] = [
  {
    key: 'product-catalog',
    label: 'Product Catalog latency',
    service: 'productcatalogservice',
    bundle: () =>
      bundle(
        'productcatalogservice',
        'INC-DEMO-PCAT',
        'Sev-2',
        'Product catalog GetProduct p95 latency above 5s',
        'The flagd feature flag productCatalogFailure is on, injecting a ~5s delay into GetProduct. Reported by oncall@example.com from 10.0.0.5.',
        'productCatalogFailure',
      ),
  },
  {
    key: 'payment',
    label: 'Payment 100% errors',
    service: 'payment',
    bundle: () =>
      bundle(
        'payment',
        'INC-DEMO-PAY',
        'Sev-1',
        'Payment Charge error rate at 100%',
        'flagd flag paymentFailure is at 100%, causing the payment Charge handler to return an error for every call.',
        'paymentFailure',
      ),
  },
  {
    key: 'cart',
    label: 'Cart 5xx',
    service: 'cart',
    bundle: () =>
      bundle(
        'cart',
        'INC-DEMO-CART',
        'Sev-1',
        'Cart error rate at 100% (HTTP 5xx)',
        'flagd flag cartFailure is on; the cart service returns a 5xx on every request.',
        'cartFailure',
      ),
  },
];

const STATUS_META: Record<ReviewStatus, { label: string; cls: string; icon: typeof Clock }> = {
  draft: { label: 'Draft', cls: 'border-ink-300 bg-ink-100 text-ink-600 dark:border-ink-600 dark:bg-ink-800 dark:text-ink-300', icon: FileText },
  pending_review: { label: 'Pending review', cls: 'border-amber-400/50 bg-amber-400/10 text-amber-600 dark:text-amber-400', icon: Clock },
  published: { label: 'Published', cls: 'border-emerald-400/50 bg-emerald-400/10 text-emerald-600 dark:text-emerald-400', icon: CheckCircle2 },
  rejected: { label: 'Rejected', cls: 'border-red-400/50 bg-red-400/10 text-red-600 dark:text-red-400', icon: XCircle },
};

function StatusBadge({ status }: { status: ReviewStatus }) {
  const m = STATUS_META[status] ?? STATUS_META.draft;
  const Icon = m.icon;
  return (
    <span className={clsx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium', m.cls)}>
      <Icon className="h-3 w-3" />
      {m.label}
    </span>
  );
}

type FlowState = 'idle' | 'inject' | 'apply' | 'close' | 'publish' | 'done' | 'error';

const normService = (s: string) =>
  (s || '').toLowerCase().replace(/[-_ ]/g, '').replace(/service$/, '');

// Drives the real 3-approval incident lifecycle for a scenario:
//   apply-fix (1st HITL) → verifier close (2nd HITL) → auto-synthesize → publish (3rd HITL).
// Each Required approval is granted by a human in the HITL console; this just
// kicks off the chain and tracks its progress by polling the shared outcome
// stores + the KB list (the draft appears once the ticket is closed).
function FullFlowRunner({ scenario, onChanged }: { scenario: DemoScenario; onChanged: () => void }) {
  const [state, setState] = useState<FlowState>('idle');
  const [applyId, setApplyId] = useState<string | null>(null);
  const [publishId, setPublishId] = useState<string | null>(null);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [failedStep, setFailedStep] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Ids of drafts that already existed when the flow started, so step-2→3 latches
  // onto the NEW draft this flow creates — not a stale/concurrent one, and with no
  // dependence on comparing a browser clock against a server timestamp.
  const seenIds = useRef<Set<number>>(new Set());

  const fail = (step: number, msg: string) => {
    setFailedStep(step);
    setErr(msg);
    setState('error');
  };

  const reset = () => {
    setState('idle');
    setApplyId(null);
    setPublishId(null);
    setDraftId(null);
    setFailedStep(null);
    setErr(null);
    seenIds.current = new Set();
  };

  const start = async () => {
    const b = scenario.bundle() as {
      rca_verdict?: {
        ranked_fix_steps?: Array<{ flag?: string; variant?: string; action_type?: string }>;
      };
    };
    const fix = b.rca_verdict?.ranked_fix_steps?.[0] ?? {};
    if (!fix.flag) {
      fail(1, 'scenario has no fix flag to apply');
      return;
    }
    setErr(null);
    setFailedStep(null);
    setDraftId(null);
    setPublishId(null);
    setApplyId(null);
    setState('inject');
    try {
      // Snapshot existing drafts so step-2→3 latches onto the NEW one this flow
      // creates, not a pre-existing/concurrent draft.
      try {
        const existing = await api.listKb({ status: 'pending_review', limit: 100 });
        seenIds.current = new Set((existing.articles ?? []).map((a) => a.id));
      } catch {
        seenIds.current = new Set();
      }
      // 0. Inject the REAL failure so triage opens a real ServiceNow incident.
      const list = await api.scenarios();
      const match = (list.scenarios ?? []).find((s) => s.flag === fix.flag);
      if (!match) {
        fail(1, `no server scenario registered for flag ${fix.flag}`);
        return;
      }
      await api.injectScenario(match.scenario_id);
      // 0b. Wait (best-effort) for triage to open the incident — apply-fix then
      // recovers the real open incident by service, so no fake id is passed.
      for (let i = 0; i < 12; i++) {
        await new Promise((r) => setTimeout(r, 2500));
        try {
          const vs = await api.verdicts({ limit: 10 });
          const hit = (vs.verdicts ?? []).some(
            (v) => normService(v.affected_service ?? '') === normService(scenario.service),
          );
          if (hit) break;
        } catch {
          /* keep waiting */
        }
      }
      // 1. Apply the fix on the REAL incident (recovered by service, not a canned id).
      setState('apply');
      const res = await api.applyRcaFix(
        fix.flag,
        fix.variant ?? 'off',
        fix.action_type ?? 'set_flag',
        'Approve the RCA fix step (1st of 3 HITL approvals).',
        { service: scenario.service, rca_verdict: b.rca_verdict, timeout_seconds: 600 },
      );
      setApplyId(res.approval_id);
    } catch (e) {
      fail(1, e instanceof Error ? e.message : String(e));
    }
  };

  // 1st HITL — apply the fix.
  useEffect(() => {
    if (state !== 'apply' || !applyId) return;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const o = await api.hitlOutcome(applyId);
        if (!alive) return;
        if (o.status === 'executed') setState('close');
        else if (o.status !== 'pending')
          fail(1, `apply-fix ${o.status}${o.error ? `: ${o.error}` : ''}`);
      } catch {
        /* transient — keep polling */
      }
    }, 2500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [state, applyId]);

  // 2nd HITL — the close-ticket card fires in the console; once the ticket is
  // closed the verifier synthesizes a draft. Watch for it to appear.
  useEffect(() => {
    if (state !== 'close') return;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const res = await api.listKb({ status: 'pending_review', limit: 50 });
        if (!alive) return;
        // Match the NEW draft for this service (stable fields, no clock math):
        // not present at flow start, and same normalized service as the scenario.
        const fresh = (res.articles ?? []).find(
          (a) =>
            !seenIds.current.has(a.id) &&
            normService(a.service ?? '') === normService(scenario.service),
        );
        if (fresh) {
          setDraftId(fresh.id);
          setState('publish');
          onChanged();
        }
      } catch {
        /* keep polling */
      }
    }, 2500);
    return () => {
      alive = false;
      clearInterval(t);
    };
    // onChanged intentionally omitted — refetch identity may change per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, scenario.service]);

  // 3rd HITL — publish the KB article.
  useEffect(() => {
    if (state !== 'publish' || !publishId) return;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const o = await api.kbPublishOutcome(publishId);
        if (!alive) return;
        if (o.status === 'published') {
          setState('done');
          onChanged();
        } else if (o.status !== 'pending') {
          fail(3, `publish ${o.status}${o.error ? `: ${o.error}` : ''}`);
        }
      } catch {
        /* keep polling */
      }
    }, 2500);
    return () => {
      alive = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, publishId]);

  const requestPublish = async () => {
    if (!draftId) return;
    try {
      const res = await api.publishKb(draftId);
      setPublishId(res.approval_id);
    } catch (e) {
      fail(3, e instanceof Error ? e.message : String(e));
    }
  };

  const pos =
    state === 'inject' || state === 'apply'
      ? 1
      : state === 'close'
        ? 2
        : state === 'publish'
          ? 3
          : state === 'done'
            ? 4
            : 0;
  const statusOf = (idx: number): 'done' | 'active' | 'pending' | 'error' => {
    if (state === 'error') {
      if (failedStep === idx) return 'error';
      if (failedStep !== null && idx < failedStep) return 'done';
      return 'pending';
    }
    if (state === 'done') return 'done';
    if (idx < pos) return 'done';
    if (idx === pos) return 'active';
    return 'pending';
  };

  const running = state === 'inject' || state === 'apply' || state === 'close' || state === 'publish';

  return (
    <div className="rounded-lg border border-ink-200 bg-ink-50/50 p-3 dark:border-ink-700 dark:bg-ink-800/30">
      <div className="flex flex-wrap items-center gap-3">
        <ListChecks className="h-5 w-5 flex-shrink-0 text-accent" />
        <span className="text-sm font-medium text-ink-700 dark:text-ink-200">
          Run the full incident flow — 3 approvals
        </span>
        {state === 'idle' || state === 'error' ? (
          <button
            onClick={start}
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-accent-hover"
          >
            <Play className="h-4 w-4" />
            {state === 'error' ? 'Restart flow' : 'Run full flow'}
          </button>
        ) : (
          <button
            onClick={reset}
            className="inline-flex items-center gap-1.5 rounded-lg border border-ink-200 px-3 py-1.5 text-sm text-ink-600 hover:bg-ink-100 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800"
          >
            Cancel
          </button>
        )}
        {state === 'done' && (
          <span className="inline-flex items-center gap-1 text-sm font-medium text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="h-4 w-4" /> Published — all 3 approvals granted
          </span>
        )}
        <a
          href="/hitl"
          target="_blank"
          rel="noreferrer"
          className="ml-auto inline-flex items-center gap-1 text-xs text-accent hover:underline"
        >
          <Gavel className="h-3.5 w-3.5" /> HITL console <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      <ol className="mt-3 space-y-2">
        <FlowStepRow
          n={1}
          status={statusOf(1)}
          title="Apply fix"
          cap="rca.fix_step.execute"
          hint={
            state === 'inject'
              ? 'Injecting the failure & waiting for triage to open the ticket…'
              : 'Approve the fix-step card in the HITL console.'
          }
        />
        <FlowStepRow
          n={2}
          status={statusOf(2)}
          title="Close ticket"
          cap="itsm.ticket.close"
          hint="Approve the “close ticket?” card; the verifier then synthesizes the draft."
        />
        <FlowStepRow
          n={3}
          status={statusOf(3)}
          title="Publish KB article"
          cap="knowledge.publish"
          hint={state === 'publish' && !publishId ? undefined : 'Approve the publish card in the HITL console.'}
        >
          {state === 'publish' && !publishId && (
            <button
              onClick={requestPublish}
              className="mt-1 inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1 text-xs font-medium text-white hover:bg-accent-hover"
            >
              <Gavel className="h-3.5 w-3.5" /> Request publish approval
            </button>
          )}
        </FlowStepRow>
      </ol>

      {err && <p className="mt-2 text-sm text-red-500">{err}</p>}
      {running && (
        <p className="mt-2 text-[11px] text-ink-400 dark:text-ink-500">
          This runs the real pipeline: it injects the failure, triage opens a real ServiceNow
          ticket, and each step is a Required HITL approval. Needs the cluster + ServiceNow up. If
          the close step stalls, the verifier hasn’t seen recovery yet (or no ticket opened) — give
          it a moment, or use “Synthesize offline” below.
        </p>
      )}
    </div>
  );
}

function FlowStepRow({
  n,
  status,
  title,
  cap,
  hint,
  children,
}: {
  n: number;
  status: 'done' | 'active' | 'pending' | 'error';
  title: string;
  cap: string;
  hint?: string;
  children?: ReactNode;
}) {
  const Icon =
    status === 'done'
      ? CheckCircle2
      : status === 'error'
        ? XCircle
        : status === 'active'
          ? Loader2
          : Clock;
  const tone =
    status === 'done'
      ? 'text-emerald-600 dark:text-emerald-400'
      : status === 'error'
        ? 'text-red-500'
        : status === 'active'
          ? 'text-accent'
          : 'text-ink-400 dark:text-ink-500';
  return (
    <li className="flex items-start gap-2 text-sm">
      <Icon className={clsx('mt-0.5 h-4 w-4 flex-shrink-0', tone, status === 'active' && 'animate-spin')} />
      <div>
        <span className="font-medium text-ink-700 dark:text-ink-200">
          {n}. {title}
        </span>{' '}
        <span className="font-mono text-[11px] text-ink-400 dark:text-ink-500">{cap}</span>
        {status === 'active' && hint && <p className="text-xs text-ink-500 dark:text-ink-400">{hint}</p>}
        {children}
      </div>
    </li>
  );
}

export default function Knowledge() {
  const list = useFetch(() => api.listKb({ limit: 50 }), { intervalMs: 5_000, cacheKey: 'kb-list' });
  const [scenario, setScenario] = useState(SCENARIOS[0].key);
  const [synthesizing, setSynthesizing] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [lastResult, setLastResult] = useState<SynthesisResult | null>(null);
  const [synthError, setSynthError] = useState<string | null>(null);
  const [dayFilter, setDayFilter] = useState('');  // "YYYY-MM-DD"

  const sc = SCENARIOS.find((s) => s.key === scenario) ?? SCENARIOS[0];

  const articles = list.data?.articles ?? [];
  const dateFiltered = dayFilter
    ? articles.filter((a) => (a.created_at ?? '').slice(0, 10) === dayFilter)
    : articles;
  const filterActive = Boolean(dayFilter);

  async function onSynthesize() {
    setSynthesizing(true);
    setSynthError(null);
    try {
      // The offline path explicitly bypasses the ticket-closed gate.
      const res = await api.synthesize(sc.bundle(), true);
      setLastResult(res);
      await list.refetch();
    } catch (e) {
      setSynthError(e instanceof Error ? e.message : String(e));
    } finally {
      setSynthesizing(false);
    }
  }

  async function onClear() {
    setClearing(true);
    setSynthError(null);
    try {
      await api.resetKb();
      setLastResult(null);
      await list.refetch();
    } catch (e) {
      setSynthError(e instanceof Error ? e.message : String(e));
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            Knowledge Synthesizer
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            PRS-007 · turns a resolved incident into a postmortem, a runbook suggestion, and a
            review-gated KB article.
          </p>
        </div>
        <a
          href="/hitl"
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-2 rounded-lg border border-ink-200 px-3 py-1.5 text-sm text-ink-600 hover:bg-ink-100 hover:text-ink-900 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-ink-50"
        >
          <ShieldCheck className="h-4 w-4" /> HITL approvals <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      {/* Synthesize panel */}
      <div className="card">
        <div className="card-body space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <Sparkles className="h-5 w-5 flex-shrink-0 text-accent" />
            <span className="text-sm font-medium text-ink-700 dark:text-ink-200">
              Resolved incident
            </span>
            <select
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
              className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm text-ink-800 dark:border-ink-700 dark:bg-ink-800 dark:text-ink-100"
            >
              {SCENARIOS.map((s) => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
            <div className="flex-1" />
            <button
              onClick={onClear}
              disabled={clearing || synthesizing}
              title="Clear all KB articles (demo reset)"
              className="inline-flex items-center gap-1.5 rounded-lg border border-ink-200 px-3 py-1.5 text-sm text-ink-600 transition-colors hover:bg-ink-100 hover:text-ink-900 disabled:opacity-60 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-ink-50"
            >
              <Trash2 className="h-4 w-4" />
              {clearing ? 'Clearing…' : 'Clear'}
            </button>
          </div>

          {/* Primary path — the real 3-approval lifecycle */}
          <FullFlowRunner scenario={sc} onChanged={list.refetch} />

          {/* Secondary path — offline draft, skips the first two approvals */}
          <div className="space-y-2 border-t border-ink-200 pt-3 dark:border-ink-700">
            <div className="flex flex-wrap items-center gap-3">
              <button
                onClick={onSynthesize}
                disabled={synthesizing || clearing}
                className="inline-flex items-center gap-1.5 rounded-lg border border-ink-200 px-3 py-1.5 text-sm text-ink-600 transition-colors hover:bg-ink-100 hover:text-ink-900 disabled:cursor-not-allowed disabled:opacity-60 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-ink-50"
              >
                <Sparkles className="h-4 w-4" />
                {synthesizing ? 'Synthesizing…' : 'Synthesize offline'}
              </button>
              <span className="text-xs text-ink-400 dark:text-ink-500">
                Offline demo — drafts directly, skipping the remediation (1st HITL) and
                close-ticket (2nd HITL) approvals; only the publish approval remains.
              </span>
            </div>
            {synthError && <p className="text-sm text-red-500">{synthError}</p>}
            {lastResult && <ResultBanner res={lastResult} />}
          </div>
        </div>
      </div>

      {/* KB article list */}
      {list.loading && !list.data ? (
        <div className="card"><LoadingState label="Loading KB articles…" /></div>
      ) : list.error && !list.data ? (
        <ErrorState error={list.error} />
      ) : articles.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No KB articles yet"
            hint="Synthesize a resolved incident above — the drafted article lands here as 'pending review'."
            icon={<BookOpen className="h-7 w-7" />}
          />
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-200 px-4 py-3 dark:border-ink-700">
            <div className="flex items-center gap-2">
              <BookOpen className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-ink-900 dark:text-ink-50">Knowledge base</h2>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Calendar className="h-4 w-4 text-ink-400" />
              <input
                type="date"
                value={dayFilter}
                onChange={(e) => setDayFilter(e.target.value)}
                title="Filter by date"
                className="rounded-lg border border-ink-200 bg-white px-2 py-1 text-xs text-ink-700 dark:border-ink-700 dark:bg-ink-800 dark:text-ink-200"
              />
              {filterActive && (
                <button
                  onClick={() => setDayFilter('')}
                  className="rounded-lg border border-ink-200 px-2 py-1 text-xs text-ink-600 hover:bg-ink-100 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800"
                >
                  All dates
                </button>
              )}
              <span className="rounded-full bg-ink-100 px-2.5 py-0.5 text-[11px] font-medium text-ink-500 dark:bg-ink-800 dark:text-ink-300">
                {dateFiltered.length}{filterActive ? ` of ${articles.length}` : ''} article{dateFiltered.length === 1 ? '' : 's'}
              </span>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead className="bg-ink-50 dark:bg-ink-800/50">
                <tr className="border-b border-ink-200 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-ink-400 dark:border-ink-700 dark:text-ink-500">
                  <th className="px-4 py-2.5">Status</th>
                  <th className="px-4 py-2.5">Article</th>
                  <th className="px-4 py-2.5">Service</th>
                  <th className="px-4 py-2.5">Incident</th>
                  <th className="px-4 py-2.5">Runbook</th>
                  <th className="px-4 py-2.5">Quality</th>
                  <th className="px-4 py-2.5">Updated</th>
                  <th className="px-4 py-2.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {dateFiltered.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="px-4 py-8 text-center text-sm text-ink-500 dark:text-ink-400">
                      No articles for the selected date.
                    </td>
                  </tr>
                ) : (
                  dateFiltered.map((a) => (
                    <ArticleRow key={a.id} a={a} onChanged={list.refetch} />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function QualityScore({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const tone =
    pct >= 70
      ? 'text-emerald-600 dark:text-emerald-400'
      : pct >= 40
        ? 'text-amber-600 dark:text-amber-400'
        : 'text-red-600 dark:text-red-400';
  return <span className={clsx('font-mono text-xs font-semibold', tone)}>{pct}%</span>;
}

function ResultBanner({ res }: { res: SynthesisResult }) {
  const dedupNote =
    res.dedup_action === 'skip_idempotent'
      ? 'already synthesized (idempotent skip)'
      : res.dedup_action === 'duplicate'
        ? `near-duplicate of #${res.dedup.matched_article_id} — not re-created`
        : 'new article created';
  return (
    <div className="rounded-lg border border-accent/30 bg-accent/5 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={res.status} />
        <span className="font-mono text-xs text-ink-600 dark:text-ink-300">{res.affected_service}</span>
        <span className="chip">{dedupNote}</span>
        <span className="chip">runbook: {res.runbook_mode} → {res.related_runbook_id}</span>
        <span className="chip">quality {Math.round(res.quality_score * 100)}%</span>
        <span className="chip">{res.redaction_summary}</span>
      </div>
      <p className="mt-2 text-ink-600 dark:text-ink-300">
        <span className="font-medium">Root cause:</span> {res.root_cause}
      </p>
    </div>
  );
}

function ArticleRow({ a, onChanged }: { a: KBArticleRow; onChanged: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [publishMsg, setPublishMsg] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const runbookMode = (a.audit_metadata?.runbook_mode as string) ?? null;

  async function onPublish() {
    setPublishing(true);
    setPublishMsg('Requested — approve on the HITL page…');
    try {
      const { approval_id } = await api.publishKb(a.id);
      // Poll the outcome while the human approves on /hitl.
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const out = await api.kbPublishOutcome(approval_id);
        if (out.status && out.status !== 'pending') {
          setPublishMsg(
            out.status === 'published'
              ? `Published${out.approver ? ` (approved by ${out.approver})` : ''}.`
              : `Publication ${out.status}.`,
          );
          await onChanged();
          break;
        }
      }
    } catch (e) {
      setPublishMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setPublishing(false);
    }
  }

  return (
    <>
      <tr className="border-b border-ink-100 align-middle hover:bg-ink-50 dark:border-ink-800 dark:hover:bg-ink-800/40">
        <td className="px-4 py-3"><StatusBadge status={a.status} /></td>
        <td className="px-4 py-3">
          <button
            onClick={() => setOpen((o) => !o)}
            className="flex max-w-md items-center gap-1.5 text-left"
            title="Show postmortem"
          >
            <ChevronRight
              className={clsx('h-3.5 w-3.5 flex-shrink-0 text-ink-400 transition-transform', open && 'rotate-90')}
            />
            <span className="truncate font-medium text-ink-900 dark:text-ink-50">{a.title}</span>
          </button>
        </td>
        <td className="px-4 py-3 font-mono text-xs text-ink-600 dark:text-ink-300">{a.service}</td>
        <td className="px-4 py-3 font-mono text-xs text-ink-500 dark:text-ink-400">
          {a.incident_id ?? '—'}
        </td>
        <td className="px-4 py-3 text-xs text-ink-500 dark:text-ink-400">
          {a.related_runbook_id ? (
            <span className="font-mono">
              {a.related_runbook_id}
              {runbookMode && <span className="ml-1 text-ink-400">({runbookMode})</span>}
            </span>
          ) : (
            '—'
          )}
        </td>
        <td className="px-4 py-3"><QualityScore score={a.quality_score} /></td>
        <td className="px-4 py-3 font-mono text-[11px] text-ink-500 dark:text-ink-400">
          {a.updated_at ? timeAgo(a.updated_at) : '—'}
        </td>
        <td className="px-4 py-3 text-right">
          {a.status === 'pending_review' ? (
            <button
              onClick={onPublish}
              disabled={publishing}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              {publishing ? 'Awaiting approval…' : 'Publish'}
            </button>
          ) : a.status === 'published' ? (
            <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5" /> {a.approved_by ?? 'published'}
            </span>
          ) : (
            <span className="text-xs text-ink-400">—</span>
          )}
        </td>
      </tr>
      {(open || publishMsg) && (
        <tr className="border-b border-ink-100 dark:border-ink-800">
          <td colSpan={8} className="bg-ink-50 px-4 py-3 dark:bg-ink-900/40">
            {publishMsg && (
              <p className="mb-2 text-xs text-ink-500 dark:text-ink-400">
                {publishMsg}
                {a.status === 'pending_review' && (
                  <>
                    {' · '}
                    <a href="/hitl" target="_blank" rel="noreferrer" className="text-accent underline">
                      open HITL page
                    </a>
                  </>
                )}
              </p>
            )}
            {open && (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-3 text-xs leading-relaxed text-ink-700 dark:bg-ink-900 dark:text-ink-200">
                {a.body}
              </pre>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
