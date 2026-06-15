import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronDown } from 'lucide-react';
import BootCurtain from '@/portal/BootCurtain';
import ExploreGuide, { GUIDE_DISMISS_KEY } from '@/portal/ExploreGuide';
import Hero from '@/portal/Hero';
import IntroUI from '@/portal/IntroUI';
import LandingSections from '@/portal/LandingSections';
import ParticleField from '@/portal/ParticleField';
import PortalNav from '@/portal/PortalNav';
import PortalProgressBar from '@/portal/PortalProgressBar';
import { usePortalProgress } from '@/portal/usePortalProgress';
import { hasEntered } from '@/lib/consoleScope';

// The Adaptive AIOps Platform Portal — landing route, v2 "Cinematic".
//
// First viewport = the immersive boot-curtain → hero reveal. Wheel/touch input
// drives boot progress (0→1) while the page scroll is LOCKED. Once boot
// completes, scroll unlocks and the marketing sections below come into view —
// turning the landing into a proper one-page site.
//
// Layer stack (low → high z) inside the first screen:
//   z-0   Hero · z-10 ParticleField · z-20 BootCurtain · z-30 ClickCatcher
//   z-50  IntroUI · z-50 PortalNav · z-60 ProgressBar

const AUTO_TRIGGER = 0.30;

export default function Landing() {
  // Returning visitors (already entered the app once) skip the cinematic boot:
  // start fully booted with the hero already revealed — no animation replay.
  const skipIntro = hasEntered();
  const { progress, setProgress, onWheel } = usePortalProgress(skipIntro ? 1 : 0);
  const navigate = useNavigate();
  const booted = progress >= 0.999;
  const [guideOpen, setGuideOpen] = useState(false);

  // Latch the hero reveal once the curtain has cleared, so the (memoised) Hero
  // re-renders exactly once instead of on every boot-progress frame.
  const [heroRevealed, setHeroRevealed] = useState(skipIntro);
  useEffect(() => {
    if (progress >= 0.95 && !heroRevealed) setHeroRevealed(true);
  }, [progress, heroRevealed]);

  // Clicking "Explore Agents" opens the quick-guide window — unless the user
  // ticked "don't show again", in which case go straight to the agent browser.
  const onExplore = useCallback(() => {
    if (localStorage.getItem(GUIDE_DISMISS_KEY) === '1') navigate('/agents');
    else setGuideOpen(true);
  }, [navigate]);

  useEffect(() => {
    const prevBg = document.body.style.backgroundColor;
    document.body.style.backgroundColor = '#050505';
    return () => { document.body.style.backgroundColor = prevBg; };
  }, []);

  // Lock page scroll until the boot sequence finishes, then release it so the
  // sections below the hero become reachable.
  useEffect(() => {
    document.body.style.overflow = booted ? '' : 'hidden';
    return () => { document.body.style.overflow = ''; };
  }, [booted]);

  const bootActive = progress < 0.9;
  const handleAdvance = () => {
    if (progress < AUTO_TRIGGER) setProgress(AUTO_TRIGGER);
  };

  return (
    <div
      onWheel={onWheel}
      className="portal-deepspace relative w-full overflow-x-hidden font-body text-white"
    >
      <PortalNav progress={progress} />

      {/* ── First screen: immersive boot + hero ── */}
      <section className="relative h-screen w-full overflow-hidden">
        <Hero revealed={heroRevealed} onExplore={onExplore} />
        <ParticleField progress={progress} />
        <BootCurtain progress={progress} />

        {bootActive && (
          <button
            type="button"
            onClick={handleAdvance}
            aria-label="Initialise system"
            tabIndex={-1}
            className="absolute inset-0 z-30 cursor-pointer bg-transparent"
            style={{ outline: 'none' }}
          />
        )}

        <IntroUI progress={progress} onSkip={handleAdvance} />
        <PortalProgressBar progress={progress} />

        {/* Scroll cue — appears once boot completes. */}
        <div
          className={`pointer-events-none absolute inset-x-0 bottom-6 z-40 flex flex-col items-center gap-1 transition-opacity duration-700 ${
            booted ? 'opacity-100' : 'opacity-0'
          }`}
        >
          <span className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
            Scroll
          </span>
          <ChevronDown className="h-4 w-4 animate-scroll-bounce text-white/50" />
        </div>
      </section>

      {/* ── Scrollable content sections ── */}
      <LandingSections onExplore={onExplore} />

      <ExploreGuide open={guideOpen} onClose={() => setGuideOpen(false)} />
    </div>
  );
}
