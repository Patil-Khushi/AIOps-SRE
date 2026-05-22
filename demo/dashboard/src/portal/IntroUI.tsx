import { clamp01 } from './easing';
import PhaseDots from './PhaseDots';
import StatusTicker from './StatusTicker';

// Cold-boot HUD. Sits above the curtain so it reads as the system's own
// status panel before any input arrives.
//   - 4 phase status dots (live colour-encoded data — they MEAN something)
//   - Typewriter status ticker (5 lines, ends on the action prompt)
//   - Scroll glyph with an indigo glow halo
// All three fade out together by progress = 0.20, translating upward.

interface IntroUIProps {
  progress: number;
  onSkip: () => void;
}

const FADE_END = 0.20;

export default function IntroUI({ progress, onSkip }: IntroUIProps) {
  const t = clamp01(progress / FADE_END);
  const opacity = 1 - t;
  const translateY = -60 * t;

  if (progress >= FADE_END) return null;

  return (
    <div
      className="fixed inset-x-0 bottom-24 z-50 flex justify-center"
      style={{
        opacity,
        transform: `translateY(${translateY}px)`,
        transition: 'opacity 200ms cubic-bezier(0.65, 0, 0.35, 1)',
      }}
    >
      <button
        type="button"
        onClick={onSkip}
        className="group flex cursor-pointer flex-col items-center gap-4 bg-transparent p-4"
        aria-label="Initialise system"
      >
        <div
          className="flex h-10 w-6 items-start justify-center rounded-full border border-white/40 transition-all group-hover:border-white/80"
          style={{ boxShadow: '0 0 12px rgba(99, 102, 241, 0.50)' }}
        >
          <span className="scroll-dot mt-1.5 block h-1.5 w-0.5 rounded-full bg-white/80" />
        </div>

        <div className="flex items-center gap-3">
          <PhaseDots />
          <StatusTicker />
        </div>
      </button>
    </div>
  );
}
