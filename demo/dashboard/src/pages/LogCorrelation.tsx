import { useEffect, useMemo, useState } from 'react';
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
import { useFetch } from '@/hooks/useFetch';
import { EmptyState } from '@/components/states';
import type {
  CorrelationResult,
  CorrelatedSignal,
  SignalSource,
  EvidenceProvenance,
  DependencyGraph,
  DependencyGraphNode,
  ConfidenceBreakdown,
  SimilarIncidents,
  ChangeContext,
  IncidentTimeline,
  CorrelationEvidence,
} from '@/types/api';
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

// The ecommerce SUT's services, named exactly as their telemetry is labelled —
// OTEL_SERVICE_NAME, the Prometheus/Jaeger `service_name`, and the Loki stream
// label are all this same string, which is what makes a correlation across the
// three sources join at all. Each one carries failure scenarios in
// demo/ecommerce/scenarios/.
//
// mock-payment-gateway is deliberately absent: it emits neither logs nor spans
// (no logging_config, no OTel — see demo/ecommerce/mock-payment-gateway/), so
// correlating on it would only ever return the synthetic fallback and read as a
// broken page rather than an honestly empty one.
const PRESET_SERVICES = [
  'ecommerce-user-service',
  'ecommerce-order-service',
  'ecommerce-payment-service',
  'ecommerce-frontend',
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

// A short label + value row, used by the panels below for provenance lines.
function MetaRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="shrink-0 text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-500">
        {label}
      </span>
      <span className="min-w-0 flex-1 text-[11px] text-ink-700 dark:text-ink-200">{value}</span>
    </div>
  );
}

// Coverage notes are the honesty mechanism across this agent's seams: they say
// why a result is incomplete, so an empty list is never misread as "nothing
// there". Rendered wherever one is present rather than tucked away.
function CoverageNote({ note }: { note?: string | null }) {
  if (!note) return null;
  return (
    <p className="mt-1.5 rounded border border-warn/30 bg-warn/5 px-2 py-1 text-[11px] leading-relaxed text-ink-600 dark:text-ink-300">
      {note}
    </p>
  );
}

// Depth-grouped rather than a node-link drawing: depth and relation are the
// fields the graph actually carries, and a circular layout hides both once the
// walk goes past one hop.
function DependencyGraphPanel({ graph }: { graph: DependencyGraph }) {
  const byDepth = new Map<number, DependencyGraphNode[]>();
  for (const n of graph.nodes) {
    const list = byDepth.get(n.depth) ?? [];
    list.push(n);
    byDepth.set(n.depth, list);
  }
  const depths = [...byDepth.keys()].sort((a, b) => a - b);
  const outgoing = (svc: string) => graph.edges.filter((e) => e.source === svc).map((e) => e.target);

  return (
    <div>
      <p className="card-title !text-[10px]">
        Dependency graph ({graph.nodes.length} nodes · {graph.edges.length} edges)
      </p>
      <div className="mt-1.5 space-y-1">
        <MetaRow label="root" value={graph.root} />
        <MetaRow
          label="depth"
          value={`${graph.max_depth_reached}${graph.truncated ? ' (truncated — a cap was hit)' : ''}`}
        />
        <MetaRow label="via" value={graph.provider ?? 'unknown'} />
        <MetaRow
          label="upstream"
          // An empty upstream list is "unknown", not "nothing calls this" — the
          // provider cannot observe callers. Saying "none" would be a factual error.
          value={graph.upstream.length > 0 ? graph.upstream.join(', ') : 'not observable from this provider'}
        />
      </div>

      <ol className="mt-2 space-y-2">
        {depths.map((depth) => (
          <li key={depth}>
            <p className="text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-500">
              depth {depth}
            </p>
            <ul className="mt-1 space-y-1">
              {byDepth.get(depth)!.map((n) => {
                const targets = outgoing(n.service);
                return (
                  <li
                    key={n.service}
                    className="rounded bg-ink-100 px-2 py-1 font-mono text-[11px] dark:bg-ink-900"
                  >
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span
                        className={clsx(
                          'font-medium',
                          n.relation === 'root'
                            ? 'text-accent'
                            : 'text-ink-800 dark:text-ink-100',
                        )}
                      >
                        {n.service}
                      </span>
                      <span className="text-ink-400 dark:text-ink-500">{n.relation}</span>
                      {n.health && <span className="text-ink-500">{n.health}</span>}
                    </div>
                    {targets.length > 0 && (
                      <p className="mt-0.5 text-ink-500 dark:text-ink-400">→ {targets.join(', ')}</p>
                    )}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ol>
      <CoverageNote note={graph.coverage_note} />
    </div>
  );
}

// The unapplied rules are the useful half: they turn "confidence is 0.3" into
// "confidence is 0.3 BECAUSE only one source carried evidence", which is
// actionable where a bare number is not.
function ConfidenceBreakdownPanel({ breakdown }: { breakdown: ConfidenceBreakdown }) {
  return (
    <div>
      <p className="card-title !text-[10px]">Confidence breakdown</p>
      <div className="mt-1.5 space-y-1">
        <MetaRow
          label="score"
          value={`${breakdown.score.toFixed(2)} (base ${breakdown.base.toFixed(2)})${breakdown.capped ? ' · capped' : ''}`}
        />
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-ink-600 dark:text-ink-300">
        {breakdown.explanation}
      </p>

      {breakdown.contributors.length > 0 && (
        <ul className="mt-2 space-y-1">
          {breakdown.contributors.map((c) => (
            <li key={c.rule_id} className="text-[11px] text-good">
              + {c.delta != null ? c.delta.toFixed(2) : ''} {c.rule_id}
              {c.reason ? ` — ${c.reason}` : ''}
            </li>
          ))}
        </ul>
      )}

      {breakdown.unapplied.length > 0 && (
        <div className="mt-2">
          <p className="text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-500">
            did not apply ({breakdown.unapplied.length})
          </p>
          <ul className="mt-1 space-y-1">
            {breakdown.unapplied.map((r) => (
              <li
                key={r.rule_id}
                className="rounded bg-ink-100 px-2 py-1 text-[11px] dark:bg-ink-900"
              >
                <span className="font-mono font-medium text-ink-700 dark:text-ink-200">
                  {r.rule_id}
                </span>
                {r.potential_delta != null && (
                  <span className="ml-1.5 text-ink-400 dark:text-ink-500">
                    (worth +{r.potential_delta.toFixed(2)})
                  </span>
                )}
                <p className="mt-0.5 leading-relaxed text-ink-600 dark:text-ink-300">{r.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {breakdown.rule_trace.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
            Rule trace ({breakdown.rule_trace.length})
          </summary>
          <ol className="mt-1.5 space-y-0.5 border-l border-ink-200 pl-3 font-mono text-[11px] text-ink-600 dark:border-ink-700 dark:text-ink-300">
            {breakdown.rule_trace.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}

function SimilarIncidentsPanel({ history }: { history: SimilarIncidents }) {
  return (
    <div>
      <p className="card-title !text-[10px]">Similar past incidents ({history.matches.length})</p>
      <div className="mt-1.5 space-y-1">
        <MetaRow label="provider" value={history.provider ?? 'none answered'} />
        {history.providers_attempted.length > 0 && (
          <MetaRow label="tried" value={history.providers_attempted.join(', ')} />
        )}
        {/* A score is uninterpretable without the population searched: "nothing
            similar" across 15 rows means little, across 10,000 it means a lot. */}
        <MetaRow label="corpus" value={history.corpus_size != null ? `${history.corpus_size} incident(s)` : 'unknown'} />
      </div>

      {history.matches.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {history.matches.map((m) => (
            <li key={m.incident_id} className="rounded bg-ink-100 px-2 py-1.5 dark:bg-ink-900">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-mono text-[11px] font-medium text-ink-800 dark:text-ink-100">
                  {m.incident_id}
                </span>
                <span className="text-[11px] text-ink-500 dark:text-ink-400">
                  similarity {m.similarity_score.toFixed(2)}
                </span>
              </div>
              {m.title && (
                <p className="mt-0.5 text-[11px] text-ink-700 dark:text-ink-200">{m.title}</p>
              )}
              {m.matching_signatures.length > 0 && (
                <p className="mt-0.5 font-mono text-[10px] text-ink-500 dark:text-ink-400">
                  shared: {m.matching_signatures.join(' · ')}
                </p>
              )}
              {/* Historical fact about the PAST incident — not a claim about this one. */}
              {m.resolution?.recorded_cause && (
                <p className="mt-0.5 text-[11px] text-ink-600 dark:text-ink-300">
                  recorded cause (then): {m.resolution.recorded_cause}
                </p>
              )}
              {m.match_explanation && (
                <p className="mt-0.5 text-[10px] text-ink-500 dark:text-ink-400">
                  {m.match_explanation}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      <CoverageNote note={history.coverage_note} />
    </div>
  );
}

function ChangeContextPanel({ ctx }: { ctx: ChangeContext }) {
  return (
    <div>
      <p className="card-title !text-[10px]">What changed ({ctx.records.length})</p>
      <div className="mt-1.5 space-y-1">
        <MetaRow
          label="collected"
          value={ctx.sources_collected.length > 0 ? ctx.sources_collected.join(', ') : 'none'}
        />
        {ctx.sources_unavailable.length > 0 && (
          <MetaRow label="unavailable" value={ctx.sources_unavailable.join(', ')} />
        )}
      </div>

      {ctx.records.length > 0 && (
        // Chronological, never by suspicion — ordering by "likely culprit" would
        // smuggle the RCA agent's judgement in without accountability.
        <ul className="mt-2 space-y-1">
          {ctx.records.map((r) => (
            <li key={r.change_id} className="rounded bg-ink-100 px-2 py-1 dark:bg-ink-900">
              <div className="flex flex-wrap items-baseline gap-2 text-[11px]">
                <span className="chip">{r.change_type}</span>
                <span className="text-ink-400 dark:text-ink-500">{r.source}</span>
                {r.timestamp && (
                  <span className="text-ink-400 dark:text-ink-500">{timeAgo(r.timestamp)}</span>
                )}
              </div>
              {r.summary && (
                <p className="mt-0.5 text-[11px] text-ink-700 dark:text-ink-200">{r.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      <CoverageNote note={ctx.coverage_note} />
    </div>
  );
}

function IncidentTimelinePanel({ timeline }: { timeline: IncidentTimeline }) {
  return (
    <details>
      <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
        Grouped incident timeline ({timeline.entries.length})
      </summary>
      <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 dark:border-ink-700">
        {timeline.entries.map((e, i) => (
          <li key={e.group_id ?? i} className="text-[11px]">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-ink-400 dark:text-ink-500">{timeAgo(e.timestamp)}</span>
              <span className="chip !text-[10px]">{e.source}</span>
              {e.occurrences > 1 && (
                <span className="text-ink-400 dark:text-ink-500">×{e.occurrences}</span>
              )}
            </div>
            <p className="mt-0.5 break-all font-mono leading-relaxed text-ink-600 dark:text-ink-300">
              {e.event}
            </p>
          </li>
        ))}
      </ol>
    </details>
  );
}

function EvidencePanel({ evidence }: { evidence: CorrelationEvidence[] }) {
  return (
    <details>
      <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
        Structured evidence ({evidence.length})
      </summary>
      <ul className="mt-2 space-y-1.5">
        {evidence.map((e) => (
          <li key={e.evidence_id} className="rounded bg-ink-100 px-2 py-1.5 dark:bg-ink-900">
            <div className="flex flex-wrap items-baseline gap-2 text-[11px]">
              <span className="chip !text-[10px]">{e.source}</span>
              <span className="text-ink-400 dark:text-ink-500">{e.signal_type}</span>
              <span className="text-ink-400 dark:text-ink-500">{e.severity}</span>
              <span className="ml-auto text-ink-500 dark:text-ink-400">
                conf {e.confidence.toFixed(2)}
              </span>
            </div>
            <p className="mt-0.5 break-all font-mono text-[11px] text-ink-700 dark:text-ink-200">
              {e.normalized_signature}
            </p>
            {e.supporting_telemetry?.sources_agreeing &&
              e.supporting_telemetry.sources_agreeing.length > 0 && (
                <p className="mt-0.5 text-[10px] text-ink-500 dark:text-ink-400">
                  agreeing: {e.supporting_telemetry.sources_agreeing.join(', ')}
                  {e.supporting_telemetry.occurrences != null &&
                    ` · ${e.supporting_telemetry.occurrences} occurrence(s)`}
                </p>
              )}
            {e.topology_context?.implicated_service && (
              <p className="mt-0.5 text-[10px] text-ink-500 dark:text-ink-400">
                topology: {e.topology_context.relation} → {e.topology_context.implicated_service}
                {e.topology_context.depth != null && ` (depth ${e.topology_context.depth})`}
              </p>
            )}
          </li>
        ))}
      </ul>
    </details>
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

      {/* Each of these is opt-in behind its own env flag, so null means "not
          attempted" and the panel is omitted entirely rather than rendering a
          misleading empty state. */}
      {result.dependency_graph && <DependencyGraphPanel graph={result.dependency_graph} />}
      {result.confidence_breakdown && (
        <ConfidenceBreakdownPanel breakdown={result.confidence_breakdown} />
      )}
      {result.similar_incidents && <SimilarIncidentsPanel history={result.similar_incidents} />}
      {result.deployment_context && <ChangeContextPanel ctx={result.deployment_context} />}
      {result.incident_timeline && <IncidentTimelinePanel timeline={result.incident_timeline} />}
      {result.evidence && result.evidence.length > 0 && (
        <EvidencePanel evidence={result.evidence} />
      )}

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
  const [service, setService] = useState(PRESET_SERVICES[0]);
  const [result, setResult] = useState<CorrelationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only offer services that actually have a fault injected right now.
  // Correlating a healthy service can only ever return the synthetic fallback,
  // which reads as a broken page rather than an honestly empty one — the same
  // reasoning that keeps mock-payment-gateway out of PRESET_SERVICES above.
  //
  // Polled, not fetched once: faults are injected and recovered while this page
  // is open, so the list has to follow the cluster. No cacheKey — a stale
  // "affected" list from localStorage would point at an already-recovered fault.
  const { data: scenarios } = useFetch(() => api.scenarios(), { intervalMs: 5000 });

  const affected = useMemo(() => {
    // current_variant is not just on/off (see the Scenario type), so anything
    // other than "off" counts as injected.
    const live = new Set(
      (scenarios?.scenarios ?? [])
        .filter((s) => s.current_variant !== 'off')
        // Scenario.service is the k8s/short name ("user-service"); PRESET_SERVICES
        // carries the telemetry label ("ecommerce-user-service").
        .map((s) => `ecommerce-${s.service}`),
    );
    return PRESET_SERVICES.filter((svc) => live.has(svc));
  }, [scenarios]);

  // Fall back to the full list when nothing is injected, otherwise the picker
  // empties out and the console looks broken on an idle cluster.
  const visibleServices = useMemo(
    () => (affected.length > 0 ? affected : PRESET_SERVICES),
    [affected],
  );

  // Keep the selection inside the visible list — otherwise the Correlate button
  // would still name a service the operator can no longer see or click.
  useEffect(() => {
    if (!visibleServices.includes(service)) setService(visibleServices[0]);
  }, [visibleServices, service]);

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
              <p className="text-xs text-ink-500 dark:text-ink-400">
                {affected.length > 0
                  ? `Showing ${affected.length} service${affected.length > 1 ? 's' : ''} with an active failure.`
                  : 'No failure injected — showing all services.'}
              </p>
              <ul className="space-y-2">
                {visibleServices.map((svc) => (
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
