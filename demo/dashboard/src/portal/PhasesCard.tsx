// The phases card — a glassmorphism panel listing the four AIOps maturity
// phases. Mirrors the agent-catalog phase summary (see CLAUDE.md):
// Reactive-Active (8) → Proactive (7) → Predictive (7) → Prescriptive (6).
// Prescriptive is 6: Remediation Recommender + Auto-Healer are merged into the
// RCA Agent (RCA now generates, recommends, and applies the fix end to end).

const PHASES = [
  {
    name: 'Reactive-Active',
    count: 8,
    swatch: '#4f46e5',
    examples: 'Alert Triage · Incident Classifier · Auto-Ticketing',
  },
  {
    name: 'Proactive',
    count: 7,
    swatch: '#7c3aed',
    examples: 'Anomaly Detector · Drift Monitor · Toil Detector',
  },
  {
    name: 'Predictive',
    count: 7,
    swatch: '#f59e0b',
    examples: 'Failure Forecaster · SLO Breach Predictor · Capacity Planner',
  },
  {
    name: 'Prescriptive-Adaptive',
    count: 6,
    swatch: '#db2777',
    examples: 'RCA Agent ★ · Chaos Orchestrator · Policy Optimizer',
  },
] as const;

export default function PhasesCard() {
  return (
    <div className="relative">
      {/* Ambient phase-coloured halos — sit behind the card, blurred. */}
      <div
        className="pointer-events-none absolute -left-16 -top-16 h-64 w-64 rounded-full opacity-60 blur-3xl"
        style={{ background: 'rgba(79, 70, 229, 0.20)' }}
      />
      <div
        className="pointer-events-none absolute -bottom-16 -right-16 h-72 w-72 rounded-full opacity-60 blur-3xl"
        style={{ background: 'rgba(245, 158, 11, 0.20)' }}
      />

      <div className="glass-card relative aspect-square rounded-3xl p-8">
        <div className="flex h-full flex-col gap-4">
          <div className="flex items-center justify-between">
            <p
              className="font-mono text-[10px] uppercase text-white/60"
              style={{ letterSpacing: '0.25em' }}
            >
              Platform Map
            </p>
            <p className="font-mono text-[10px] text-white/60">
              30 agents · 4 phases
            </p>
          </div>

          <div className="flex flex-1 flex-col justify-around">
            {PHASES.map((p) => (
              <div
                key={p.name}
                className="flex items-center justify-between gap-3 border-b border-white/10 pb-3 last:border-0 last:pb-0"
              >
                <div className="flex items-center gap-3">
                  <span
                    className="h-2.5 w-2.5 flex-shrink-0 rounded-full"
                    style={{
                      backgroundColor: p.swatch,
                      boxShadow: `0 0 12px ${p.swatch}`,
                    }}
                  />
                  <div className="flex flex-col">
                    <span
                      className="font-display text-base font-extrabold uppercase text-white"
                      style={{ letterSpacing: '-0.02em' }}
                    >
                      {p.name}
                    </span>
                    <span className="font-body text-[11px] text-white/50">
                      {p.examples}
                    </span>
                  </div>
                </div>
                <span className="rounded-full border border-white/20 bg-white/5 px-2.5 py-1 font-mono text-[11px] font-medium text-white/90">
                  {String(p.count).padStart(2, '0')} agents
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
