import { useEffect, useState } from 'react';
import { ShieldCheck } from 'lucide-react';

// Hero side-panel: the platform's outcome targets, as big animated counters.
// Numbers are the documented POC targets (KPI.md → Solution Design slide 12),
// labelled "targets" so they are honest, not claimed live results. Each tile
// is tinted with one of the four phase colours — a nod to the 4 phases without
// repeating a list. Counters animate up when the hero reveals.

interface Metric {
  display?: string;       // static value (e.g. "< 2 min")
  value?: number;         // animated value
  prefix?: string;
  suffix?: string;
  label: string;
  sub: string;
  swatch: string;
}

const METRICS: Metric[] = [
  { value: 55, prefix: '−', suffix: '%', label: 'Mean time to resolve', sub: 'vs. baseline',     swatch: '#4f46e5' },
  { display: '< 2 min',                  label: 'Mean time to acknowledge', sub: 'Sev-1 / Sev-2', swatch: '#7c3aed' },
  { value: 75, prefix: '−', suffix: '%', label: 'Alert noise cut',       sub: 'dedup + correlate', swatch: '#f59e0b' },
  { value: 99,             suffix: '%',  label: 'Notification delivery', sub: 'all sinks OK',     swatch: '#db2777' },
];

function useCountUp(target: number | undefined, run: boolean, ms = 1100) {
  const [v, setV] = useState(0);
  useEffect(() => {
    if (target === undefined) return;
    if (!run) {
      setV(0);
      return;
    }
    let raf = 0;
    let start = 0;
    const tick = (t: number) => {
      if (!start) start = t;
      const p = Math.min((t - start) / ms, 1);
      const eased = 1 - Math.pow(1 - p, 3);
      setV(target * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, run, ms]);
  return v;
}

function MetricTile({ m, run }: { m: Metric; run: boolean }) {
  const n = useCountUp(m.value, run);
  return (
    <div
      className="relative overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] p-5"
      style={{ borderTop: `2px solid ${m.swatch}` }}
    >
      <div
        className="pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full opacity-30 blur-2xl"
        style={{ background: m.swatch }}
      />
      <div
        className="relative font-display text-3xl font-black tracking-tight text-white md:text-4xl"
        style={{ letterSpacing: '-0.03em' }}
      >
        {m.display ?? `${m.prefix ?? ''}${Math.round(n)}${m.suffix ?? ''}`}
      </div>
      <p className="relative mt-2 font-body text-[13px] font-semibold text-white/85">{m.label}</p>
      <p className="relative font-body text-[11px] text-white/45">{m.sub}</p>
    </div>
  );
}

export default function OutcomesCard({ revealed = true }: { revealed?: boolean }) {
  return (
    <div className="relative">
      {/* Ambient halos for depth. */}
      <div
        className="pointer-events-none absolute -left-16 -top-16 h-64 w-64 rounded-full opacity-50 blur-3xl"
        style={{ background: 'rgba(79, 70, 229, 0.20)' }}
      />
      <div
        className="pointer-events-none absolute -bottom-16 -right-16 h-72 w-72 rounded-full opacity-50 blur-3xl"
        style={{ background: 'rgba(219, 39, 119, 0.18)' }}
      />

      <div className="glass-card relative rounded-3xl p-7">
        {/* Header */}
        <div className="flex items-center justify-between">
          <p className="font-mono text-[10px] uppercase text-white/60" style={{ letterSpacing: '0.25em' }}>
            What it delivers
          </p>
          <span className="rounded-full border border-white/20 bg-white/5 px-2.5 py-1 font-mono text-[10px] text-white/55">
            POC targets
          </span>
        </div>

        {/* Metric grid */}
        <div className="mt-5 grid grid-cols-2 gap-3">
          {METRICS.map((m) => (
            <MetricTile key={m.label} m={m} run={revealed} />
          ))}
        </div>

        {/* Trust line */}
        <div className="mt-5 flex items-center gap-2 border-t border-white/10 pt-4">
          <ShieldCheck className="h-3.5 w-3.5 flex-none text-white/50" />
          <p className="font-mono text-[10px] uppercase text-white/45" style={{ letterSpacing: '0.12em' }}>
            Vendor-neutral · HITL on every action · Policy-gated
          </p>
        </div>
      </div>
    </div>
  );
}
