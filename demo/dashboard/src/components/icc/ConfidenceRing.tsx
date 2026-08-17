import type { RootCauseStatus } from '@/types/rca';

const STATUS_COLOR: Record<RootCauseStatus, string> = {
  confirmed: 'var(--icc-ok)',
  probable: 'var(--icc-accent)',
  uncertain: 'var(--icc-unknown)',
  insufficient_evidence: 'var(--icc-unknown)',
};

// Renders confidence_score (platform-computed) as the headline — never
// llm_stated_confidence. When the model's own number is present and diverges
// from the platform's by more than the threshold, it appears only as a small
// subordinate tick + note, exactly mirroring the backend's
// _authoritative_confidence invariant: the computed score is the verdict's
// authoritative confidence, the model's number is a diagnostic footnote.
const DIVERGENCE_THRESHOLD = 0.15;

export function ConfidenceRing({
  value,
  status,
  llmStated,
  size = 96,
}: {
  value: number;
  status: RootCauseStatus;
  llmStated?: number | null;
  size?: number;
}) {
  const color = STATUS_COLOR[status];
  const stroke = size * 0.09;
  const r = size / 2 - stroke;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(1, value));
  const diverges =
    llmStated != null && Math.abs(llmStated - value) > DIVERGENCE_THRESHOLD;

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--icc-border)" strokeWidth={stroke} />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={color}
            strokeWidth={stroke}
            strokeDasharray={`${c * pct} ${c}`}
            strokeLinecap="round"
          />
          {diverges && (
            // A small secondary tick marking where the model's own (unused)
            // number would land — a diagnostic footnote, never the headline.
            <circle
              cx={size / 2 + r * Math.cos(2 * Math.PI * (llmStated as number))}
              cy={size / 2 + r * Math.sin(2 * Math.PI * (llmStated as number))}
              r={stroke * 0.4}
              fill="var(--icc-fg-faint)"
            />
          )}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-xl font-semibold tabular-nums" style={{ color }}>
            {Math.round(pct * 100)}%
          </span>
        </div>
      </div>
      <p className="text-[10px] uppercase tracking-wide text-[var(--icc-fg-muted)]">Platform confidence</p>
      {diverges && (
        <p
          className="max-w-[10rem] text-center text-[10px] text-[var(--icc-fg-faint)]"
          title="The model's own stated confidence — not used for the verdict; the platform score above is authoritative."
        >
          model claimed {Math.round((llmStated as number) * 100)}%
        </p>
      )}
    </div>
  );
}
