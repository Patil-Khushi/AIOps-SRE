// The four phase dots — first data-encoding visible to the user. Each maps
// to one of the AIOps maturity phases and uses that phase's signal colour.
// They pulse on a 1.2s loop with a 300ms stagger so the line breathes.

const DOTS: { label: string; color: string }[] = [
  { label: 'reactive',     color: '#ef4444' },
  { label: 'proactive',    color: '#f59e0b' },
  { label: 'predictive',   color: '#3b82f6' },
  { label: 'prescriptive', color: '#10b981' },
];

export default function PhaseDots() {
  return (
    <div className="flex items-center gap-2" aria-label="Phase status indicators">
      {DOTS.map((d, i) => (
        <span
          key={d.label}
          className="phase-dot inline-block h-1.5 w-1.5 rounded-full"
          style={{
            backgroundColor: d.color,
            boxShadow: `0 0 6px ${d.color}80`,
            animationDelay: `${i * 0.3}s`,
          }}
          aria-label={d.label}
        />
      ))}
    </div>
  );
}
