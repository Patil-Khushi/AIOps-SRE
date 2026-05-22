import { useEffect } from 'react';
import BootCurtain from '@/portal/BootCurtain';
import Hero from '@/portal/Hero';
import IntroUI from '@/portal/IntroUI';
import ParticleField from '@/portal/ParticleField';
import PortalNav from '@/portal/PortalNav';
import PortalProgressBar from '@/portal/PortalProgressBar';
import { usePortalProgress } from '@/portal/usePortalProgress';

// The Adaptive AIOps Platform Portal — landing route, v2 "Cinematic".
//
// Layer stack (low → high z):
//   z-0   Hero          — radial-deep-space base + vibrant ignite on burn-off
//   z-10  ParticleField — 150 stars, mesh, pulled inward → scattered
//   z-20  BootCurtain   — amber halo + indigo breath + mask + scanline + flash
//   z-30  ClickCatcher  — invisible button covers viewport during cold-boot
//   z-50  IntroUI       — phase dots + status ticker + scroll glyph (fades 0→0.20)
//   z-50  PortalNav     — letter-by-letter reveal once boot ≥ 0.95
//   z-60  ProgressBar   — 4-phase colour-narrative bottom bar
//
// Wheel events bind on the root div via React's onWheel — dodges a Chrome
// quirk where wheel on an overflow-locked viewport-sized body can be
// dropped by the compositor. usePortalProgress also keeps document-level
// wheel + window touch/keyboard listeners as backstops.

const AUTO_TRIGGER = 0.30;

export default function Landing() {
  const { progress, setProgress, onWheel } = usePortalProgress();

  useEffect(() => {
    const prevBg = document.body.style.backgroundColor;
    document.body.style.backgroundColor = '#050505';
    return () => { document.body.style.backgroundColor = prevBg; };
  }, []);

  const bootActive = progress < 0.9;
  const handleAdvance = () => {
    if (progress < AUTO_TRIGGER) setProgress(AUTO_TRIGGER);
  };

  return (
    <div
      onWheel={onWheel}
      className="portal-deepspace relative h-screen w-screen overflow-hidden font-body text-white"
    >
      <Hero progress={progress} />
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
      <PortalNav progress={progress} />
      <PortalProgressBar progress={progress} />
    </div>
  );
}
