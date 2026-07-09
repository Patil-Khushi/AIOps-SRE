import { useState } from 'react';
import {
  Layers,
  RefreshCw,
  Sparkles,
  FileText,
  Waypoints,
  Activity,
  Inbox,
  Radio,
  FlaskConical,
} from 'lucide-react';
import { api } from '@/lib/api';
import { EmptyState } from '@/components/states';
import type { CorrelationResult, CorrelatedSignal, SignalSource, EvidenceProvenance } from '@/types/api';
import { clsx, timeAgo } from '@/lib/format';

// ─── RA-007 Log Correlation console ─────────────────────────────────────────
//
// RA-007's own surface. Pick a service, run correlate(), and see the correlated
// evidence pack: a timeline color-coded by signal source (logs / traces /
// metrics), the top recurring error signatures, the suspected components, a
// confidence meter, the decision trace, and — the point of #220 — a
// `live | synthetic` provenance badge sourced from audit_metadata.signal_source
// so you can tell real multi-signal correlation from the offline fallback.
// Modeled on RcaConsole.tsx / RcaView.tsx so the two consoles don't drift.

// The demo services that carry a failure scenario (mirrors RcaView's flag map).
const PRESET_SERVICES = [
  'product-catalog',
  'cart',
  'payment',
  'recommendation',
  'ad',
  'checkout',
  'currency',
];

// Per-source visual identity for the timeline + counts.
const SOURCE_META: Record<
  SignalSource,
  { label: string; icon: typeof FileText; dot: string; chip: string }
> = {
  logs: {
    label: 'logs',
    icon: FileText,
    dot: 'bg-sky-500',
    chip: '!border-sky-500/40 !text-sky-600 dark:!text-sky-400',
  },
  traces: {
    label: 'traces',
    icon: Waypoints,
    dot: 'bg-violet-500',
    chip: '!border-violet-500/40 !text-violet-600 dark:!text-violet-400',
  },
  metrics: {
    label: 'metrics',
    icon: Activity,
    dot: 'bg-amber-500',
    chip: '!border-amber-500/40 !text-amber-600 dark:!text-amber-400',
  },
};

const ERROR_SEVERITIES = new Set(['error', 'critical', 'fatal', 'warn', 'warning']);

function ProvenanceBadge({ source }: { source: EvidenceProvenance }) {
  if (source === 'live') {
    return (
      <span className="chip !border-ok/40 !text-ok" title="Signals pulled from Loki / Jaeger / Prometheus">
        <Radio className="mr-1 inline h-3 w-3" /> live
      </span>
    );
  }
  if (source === 'mixed') {
    return (
      <span className="chip !border-warn/40 !text-warn" title="Some live signals, some synthetic">
        <Radio className="mr-1 inline h-3 w-3" /> mixed
      </span>
    );
  }
  return (
    <span
      className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400"
      title="Deterministic fallback — observability backends were unreachable"
    >
      <FlaskConical className="mr-1 inline h-3 w-3" /> synthetic
    </span>
  );
}

function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone = value >= 0.7 ? 'bg-ok' : value >= 0.45 ? 'bg-warn' : 'bg-bad';
  return (
    <div className="min-w-[8rem]">
      <div className="flex items-baseline justify-between gap-2">
        <span className="card-title !text-[10px]">Confidence</span>
        <span className="font-mono text-[11px] text-ink-600 dark:text-ink-300">{pct}%</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-ink-200 dark:bg-ink-700">
        <div className={clsx('h-full rounded-full transition-all', tone)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function SourceCounts({ timeline }: { timeline: CorrelatedSignal[] }) {
  const counts: Record<SignalSource, number> = { logs: 0, traces: 0, metrics: 0 };
  timeline.forEach((s) => (counts[s.source] += 1));
  return (
    <div className="flex flex-wrap gap-1.5">
      {(Object.keys(counts) as SignalSource[]).map((src) => {
        const M = SOURCE_META[src];
        const Icon = M.icon;
        return (
          <span key={src} className={clsx('chip', M.chip)}>
            <Icon className="mr-1 inline h-3 w-3" /> {counts[src]} {M.label}
          </span>
        );
      })}
    </div>
  );
}

function Timeline({ timeline }: { timeline: CorrelatedSignal[] }) {
  return (
    <ol className="mt-2 space-y-1.5">
      {timeline.map((s, i) => {
        const M = SOURCE_META[s.source];
        const isError = ERROR_SEVERITIES.has(s.severity.toLowerCase());
        return (
          <li
            key={i}
            className="flex items-start gap-2 rounded-md border border-ink-200 bg-ink-50/50 p-2 dark:border-ink-700 dark:bg-ink-800/30"
          >
            <span className={clsx('mt-1 h-2 w-2 flex-shrink-0 rounded-full', M.dot)} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-1.5">
                <span className={clsx('chip !py-0 !text-[9px]', M.chip)}>{M.label}</span>
                <span
                  className={clsx(
                    'font-mono text-[9px] uppercase',
                    isError ? 'text-bad' : 'text-ink-400 dark:text-ink-500',
                  )}
                >
                  {s.severity}
                </span>
                <span className="ml-auto font-mono text-[10px] text-ink-400 dark:text-ink-500">
                  {timeAgo(s.timestamp)}
                </span>
              </div>
              <p className="mt-0.5 truncate text-xs font-medium text-ink-900 dark:text-ink-50">
                {s.signature}
              </p>
              {s.sample && s.sample !== s.signature && (
                <p className="mt-0.5 truncate font-mono text-[10px] text-ink-500 dark:text-ink-400">
                  {s.sample}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function CorrelationView({ result }: { result: CorrelationResult }) {
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <ProvenanceBadge source={result.audit_metadata.signal_source} />
        <ConfidenceMeter value={result.confidence} />
      </div>

      <div>
        <p className="card-title !text-[10px]">Evidence summary</p>
        <p className="mt-1.5 leading-relaxed text-ink-900 dark:text-ink-50">{result.summary}</p>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="card-title !text-[10px]">Signals</p>
          <div className="mt-1.5">
            <SourceCounts timeline={result.timeline} />
          </div>
        </div>
        {result.suspected_dependencies.length > 0 && (
          <div>
            <p className="card-title !text-[10px]">Suspected components</p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {result.suspected_dependencies.map((d) => (
                <span key={d} className="chip !border-bad/40 !text-bad">
                  {d}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {result.top_signatures.length > 0 && (
        <div>
          <p className="card-title !text-[10px]">Top error signatures</p>
          <ol className="mt-1.5 space-y-1">
            {result.top_signatures.map((sig, i) => (
              <li
                key={i}
                className="flex items-start gap-2 rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200"
              >
                <span className="text-ink-400 dark:text-ink-500">{i + 1}.</span>
                <span className="min-w-0 flex-1">{sig}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      <div>
        <p className="card-title !text-[10px]">Timeline ({result.timeline.length})</p>
        <Timeline timeline={result.timeline} />
      </div>

      <details>
        <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
          Decision trace ({result.audit_metadata.decision_trace.length} steps)
        </summary>
        <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 font-mono text-[11px] text-ink-600 dark:border-ink-700 dark:text-ink-300">
          {result.audit_metadata.decision_trace.map((line, i) => (
            <li key={i} className="leading-relaxed">
              {i + 1}. {line}
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}

export default function LogCorrelation() {
  const [service, setService] = useState('product-catalog');
  const [result, setResult] = useState<CorrelationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (svc: string) => {
    setService(svc);
    setResult(null);
    setError(null);
    setBusy(true);
    try {
      setResult(await api.correlate(svc));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            <Layers className="h-6 w-6 text-accent" /> Log Correlation
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            RA-007 · correlates logs (Loki) + traces (Jaeger) + metrics (Prometheus) into one
            evidence pack for RCA.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        {/* Service picker */}
        <div className="lg:col-span-2">
          <div className="card">
            <div className="card-header">
              <h2 className="card-title">Service</h2>
            </div>
            <div className="card-body space-y-3">
              <ul className="space-y-2">
                {PRESET_SERVICES.map((svc) => (
                  <li key={svc}>
                    <button
                      onClick={() => run(svc)}
                      disabled={busy}
                      className={clsx(
                        'w-full rounded-md border px-3 py-2 text-left text-sm font-medium transition-colors disabled:opacity-50',
                        svc === service
                          ? '!border-accent bg-accent/5 text-accent ring-1 ring-accent/30'
                          : 'border-ink-200 hover:border-accent/50 dark:border-ink-700',
                      )}
                    >
                      {svc}
                    </button>
                  </li>
                ))}
              </ul>
              <button
                onClick={() => run(service)}
                disabled={busy}
                className="btn btn-primary w-full"
              >
                {busy ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Sparkles className="h-4 w-4" />
                )}
                {result ? 'Re-correlate' : 'Correlate'} {service}
              </button>
            </div>
          </div>
        </div>

        {/* Evidence pack */}
        <aside className="lg:col-span-3">
          <div className="card lg:sticky lg:top-20 lg:flex lg:max-h-[calc(100vh-6rem)] lg:flex-col lg:overflow-hidden">
            <div className="card-header">
              <h2 className="card-title">Correlated evidence pack</h2>
            </div>
            <div className="card-body lg:flex-1 lg:overflow-y-auto">
              {!result && !busy && !error && (
                <EmptyState
                  label={`Correlate ${service}`}
                  hint="Pull the last 15 minutes of logs, traces, and metrics for the service and correlate them into an evidence pack."
                  icon={<Inbox className="h-7 w-7" />}
                />
              )}
              {busy && (
                <div className="space-y-2 animate-pulse">
                  <div className="h-3 w-3/4 rounded bg-ink-200 dark:bg-ink-700" />
                  <div className="h-3 w-5/6 rounded bg-ink-200 dark:bg-ink-700" />
                  <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                  <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                </div>
              )}
              {error && <p className="text-sm text-bad">{error}</p>}
              {result && !busy && <CorrelationView result={result} />}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
