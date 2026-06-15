import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Compass, Rocket, Layers, X } from 'lucide-react';

// A small, skippable "how to explore" window shown when the user clicks
// "Explore Agents" on the landing page. It explains the drill-down flow
// (phase → agent → dashboard) in three steps, then drops the user into the
// agent browser. "Don't show again" persists so returning users go straight in.

export const GUIDE_DISMISS_KEY = 'aiops-explore-guide-dismissed';

const STEPS = [
  { icon: Layers,  title: 'Choose a phase',  body: 'Four maturity phases, in order: Reactive → Proactive → Predictive → Prescriptive.' },
  { icon: Compass, title: 'Open an agent',   body: 'See what it does, why it matters, and the tools you would configure.' },
  { icon: Rocket,  title: 'Press “Try it”',  body: 'Launch the agent’s live demo dashboard and watch it work.' },
];

const ACCENT = '#4f46e5';

export default function ExploreGuide({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const [dontShow, setDontShow] = useState(false);

  // Esc to close + lock background scroll while open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  const go = () => {
    if (dontShow) localStorage.setItem(GUIDE_DISMISS_KEY, '1');
    onClose();
    navigate('/agents');
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      {/* backdrop */}
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
      />

      {/* window */}
      <div className="glass-card animate-slide-up relative w-full max-w-md rounded-3xl p-7" style={{ borderTop: `3px solid ${ACCENT}` }}>
        <div
          className="pointer-events-none absolute -right-16 -top-16 h-44 w-44 rounded-full opacity-40 blur-3xl"
          style={{ background: ACCENT }}
        />

        <div className="relative">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
                Quick guide
              </p>
              <h2 className="mt-2 font-display text-2xl font-extrabold uppercase text-white" style={{ letterSpacing: '-0.02em' }}>
                How to explore
              </h2>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Skip"
              className="flex h-8 w-8 flex-none items-center justify-center rounded-full border border-white/15 text-white/60 transition-colors hover:bg-white/10 hover:text-white"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* steps */}
          <ol className="relative mt-6">
            {STEPS.map((s, i) => {
              const Icon = s.icon;
              const last = i === STEPS.length - 1;
              return (
                <li key={s.title} className="relative flex gap-4 pb-5 last:pb-0">
                  {!last && (
                    <span
                      aria-hidden
                      className="absolute left-[18px] top-10 bottom-0 w-px"
                      style={{ background: `linear-gradient(to bottom, ${ACCENT}99, ${ACCENT}22)` }}
                    />
                  )}
                  <span
                    className="relative z-10 flex h-9 w-9 flex-none items-center justify-center rounded-full"
                    style={{ backgroundColor: `${ACCENT}33`, border: `1px solid ${ACCENT}` }}
                  >
                    <Icon className="h-4 w-4 text-white" />
                  </span>
                  <div className="pt-0.5">
                    <p className="font-body text-sm font-semibold text-white">{s.title}</p>
                    <p className="mt-1 font-body text-[13px] leading-relaxed text-white/55">{s.body}</p>
                  </div>
                </li>
              );
            })}
          </ol>

          {/* actions */}
          <div className="mt-5 flex items-center justify-between gap-3 border-t border-white/10 pt-4">
            <label className="flex cursor-pointer items-center gap-2 font-body text-[12px] text-white/50">
              <input
                type="checkbox"
                checked={dontShow}
                onChange={(e) => setDontShow(e.target.checked)}
                className="h-3.5 w-3.5 accent-[#4f46e5]"
              />
              Don&apos;t show again
            </label>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={go}
                className="rounded-full px-4 py-2.5 font-body text-[11px] font-bold uppercase text-white/60 transition-colors hover:text-white"
                style={{ letterSpacing: '0.15em' }}
              >
                Skip
              </button>
              <button
                type="button"
                onClick={go}
                className="inline-flex items-center gap-2 rounded-full bg-white px-5 py-2.5 font-body text-[11px] font-bold uppercase text-black transition-colors hover:bg-white/90"
                style={{ letterSpacing: '0.15em' }}
              >
                Let&apos;s go <ArrowRight className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
