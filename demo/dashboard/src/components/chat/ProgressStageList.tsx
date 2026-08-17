import { Loader2, CheckCircle2, AlertTriangle } from 'lucide-react';
import type { RcaProgressStageVM } from '@/lib/rcaStream';

const STAGE_LABEL: Record<string, string> = {
  received: 'Reading triage verdict',
  change_correlation: 'Correlating recent changes',
  evidence: 'Gathering live evidence',
  context_pack: 'Assembling the context pack',
  memory_recall: 'Recalling verified past outcomes',
  action_vocabulary: 'Resolving executable actions',
  hypotheses: 'Scoring hypotheses',
  explaining: 'Explaining the top hypothesis',
  complete: 'Done',
  failed: 'Failed',
};

// Driven by REAL backend progress (GET /api/rca/stream/{run_id}) — the label
// shown is the server's own `label` field for the most recent frame per
// stage, not a static string, so a degraded stage's real detail ("…SCM seam
// unreachable") shows through rather than a generic name.
export function ProgressStageList({ stages }: { stages: RcaProgressStageVM[] }) {
  if (stages.length === 0) {
    return (
      <div className="flex items-center gap-2 text-xs text-[var(--icc-fg-muted)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Starting investigation…
      </div>
    );
  }
  return (
    <ul className="space-y-1">
      {stages.map((s) => {
        const done = s.outcome === 'ok' || s.outcome === 'degraded' || s.outcome === 'failed';
        const Icon = !done ? Loader2 : s.outcome === 'failed' ? AlertTriangle : CheckCircle2;
        const color =
          s.outcome === 'failed'
            ? 'var(--icc-bad)'
            : s.outcome === 'degraded'
              ? 'var(--icc-warn)'
              : done
                ? 'var(--icc-ok)'
                : 'var(--icc-fg-muted)';
        return (
          <li key={s.stage} className="flex items-center gap-2 text-xs" style={{ color }}>
            <Icon className={done ? 'h-3.5 w-3.5' : 'h-3.5 w-3.5 animate-spin'} />
            {s.label || STAGE_LABEL[s.stage] || s.stage}
          </li>
        );
      })}
    </ul>
  );
}
