import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import PhasesCard from './PhasesCard';

// The hero — what the platform "boots into".
//
// Instead of a simple opacity fade we run a 1.4s fog-burn-off CSS animation
// (see index.css `heroBurnoff`) the moment the curtain begins to fully
// clear (progress ≥ 0.95). The vibrant phase-gradient simultaneously
// "ignites" — its background-size expands from 100% (flat) to 400% over
// 2s, so the colour bloom happens as the fog clears.
//
// `pointer-events-none` until burn-off begins, so accidental clicks during
// boot can't accidentally trigger the EXPLORE AGENTS navigation.

interface HeroProps {
  progress: number;
}

const REVEAL_AT = 0.95;

export default function Hero({ progress }: HeroProps) {
  const [revealStarted, setRevealStarted] = useState(false);
  useEffect(() => {
    if (progress >= REVEAL_AT && !revealStarted) setRevealStarted(true);
  }, [progress, revealStarted]);

  return (
    <div className="portal-deepspace absolute inset-0 z-0 overflow-hidden">
      {/* Vibrant phase gradient — flat until ignition. */}
      <div
        className={`vibrant-bg absolute inset-0 opacity-30 ${revealStarted ? 'ignited' : ''}`}
      />
      <div className="absolute inset-0 bg-gradient-to-b from-black/30 via-transparent to-black/50" />

      <div
        className={`absolute inset-0 flex items-center justify-center px-8 ${
          revealStarted ? 'hero-burnoff pointer-events-auto' : 'pointer-events-none opacity-0'
        }`}
      >
        <div className="mx-auto grid w-full max-w-7xl items-center gap-12 md:grid-cols-2">
          {/* Left column: identity + CTAs */}
          <div className="flex flex-col gap-8">
            <p
              className="font-mono text-[11px] uppercase text-white/60"
              style={{ letterSpacing: '0.35em' }}
            >
              Platform · v0.1 · POC
            </p>
            <h1
              className="font-display text-6xl font-black uppercase text-white md:text-7xl lg:text-8xl"
              style={{ letterSpacing: '-0.05em', lineHeight: 0.9 }}
            >
              Adaptive
              <br />
              AIOps
              <span className="text-white/50"> +</span>
              <br />
              SRE Ops
            </h1>
            <p
              className="font-body text-base font-medium uppercase text-white/40"
              style={{ letterSpacing: '0.18em' }}
            >
              30 Agents · 4 Phases · Vendor-Neutral
            </p>
            <p className="max-w-lg font-body text-base font-normal leading-relaxed text-white/70 md:text-lg">
              Vendor-neutral, modular, governed agent-based AIOps. From the
              first alert to the executed fix — Reactive triage, Proactive
              detection, Predictive forecasting, and Prescriptive remediation,
              every action gated by policy-as-code and a human in the loop.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Link
                to="/agents"
                className="rounded-full bg-white px-6 py-3 font-body text-[12px] font-bold uppercase text-black transition-colors hover:bg-white/90"
                style={{ letterSpacing: '0.2em' }}
              >
                Explore Agents
              </Link>
              <Link
                to="/console/topology"
                className="rounded-full border border-white/30 px-6 py-3 font-body text-[12px] font-bold uppercase text-white transition-colors hover:bg-white/10"
                style={{ letterSpacing: '0.2em' }}
              >
                View Architecture
              </Link>
            </div>
          </div>

          {/* Right column: glassmorphic phases card */}
          <div className="w-full max-w-md justify-self-center md:justify-self-end">
            <PhasesCard />
          </div>
        </div>
      </div>
    </div>
  );
}
