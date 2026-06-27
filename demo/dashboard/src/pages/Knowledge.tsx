import { useState } from 'react';
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

export default function Knowledge() {
  const list = useFetch(() => api.listKb({ limit: 50 }), { intervalMs: 5_000, cacheKey: 'kb-list' });
  const [scenario, setScenario] = useState(SCENARIOS[0].key);
  const [synthesizing, setSynthesizing] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [lastResult, setLastResult] = useState<SynthesisResult | null>(null);
  const [synthError, setSynthError] = useState<string | null>(null);
  const [dayFilter, setDayFilter] = useState('');  // "YYYY-MM-DD"

  const articles = list.data?.articles ?? [];
  const dateFiltered = dayFilter
    ? articles.filter((a) => (a.created_at ?? '').slice(0, 10) === dayFilter)
    : articles;
  const filterActive = Boolean(dayFilter);

  async function onSynthesize() {
    const sc = SCENARIOS.find((s) => s.key === scenario)!;
    setSynthesizing(true);
    setSynthError(null);
    try {
      const res = await api.synthesize(sc.bundle());
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
        <div className="card-body space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <Sparkles className="h-5 w-5 flex-shrink-0 text-accent" />
            <span className="text-sm font-medium text-ink-700 dark:text-ink-200">
              Synthesize a resolved incident
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
            <button
              onClick={onSynthesize}
              disabled={synthesizing || clearing}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Sparkles className="h-4 w-4" />
              {synthesizing ? 'Synthesizing…' : 'Synthesize'}
            </button>
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
          {synthError && <p className="text-sm text-red-500">{synthError}</p>}
          {lastResult && <ResultBanner res={lastResult} />}
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
