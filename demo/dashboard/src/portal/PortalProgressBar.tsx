// 4px progress bar pinned to the bottom of the viewport.
//
// The bar's fill colour narrates the four AIOps maturity phases as the boot
// progresses — red (Reactive), amber (Proactive), blue (Predictive), green
// (Prescriptive). The colour transitions via a fixed gradient that gets
// revealed as the bar grows, so each phase boundary is visible in the
// gradient stops.

interface PortalProgressBarProps {
  progress: number;
}

// Hard stops so each phase has a discrete colour band — no muddy blends
// between phase colours.
const PHASE_GRADIENT =
  'linear-gradient(to right,' +
  ' #ef4444 0%,  #ef4444 25%,' +    // Reactive-Active
  ' #f59e0b 25%, #f59e0b 50%,' +    // Proactive
  ' #3b82f6 50%, #3b82f6 75%,' +    // Predictive
  ' #10b981 75%, #10b981 100%)';    // Prescriptive-Adaptive

export default function PortalProgressBar({ progress }: PortalProgressBarProps) {
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-0 z-[60] h-1 bg-white/5"
      role="progressbar"
      aria-label="System boot progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(progress * 100)}
    >
      {/* Full-width gradient as a backdrop; the white bar above CLIPS it. */}
      <div
        className="absolute inset-0"
        style={{ background: PHASE_GRADIENT, opacity: 0.85 }}
      />
      {/* Mask the unfilled portion with the same dark tone as the page,
          so only the filled part of the gradient is visible. */}
      <div
        className="absolute top-0 bottom-0 right-0 bg-[#050505]"
        style={{
          left: `${progress * 100}%`,
          transition: 'left 120ms cubic-bezier(0.65, 0, 0.35, 1)',
        }}
      />
    </div>
  );
}
