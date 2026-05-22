import { useEffect, useState } from 'react';
import { clamp01, maskScale } from './easing';

// The boot curtain — v2 "Cinematic" rework, perf-tuned.
//
// PERF: SVG `feGaussianBlur` is GPU-friendly *at rest* but re-rasterises
// the filtered region every time the source content (text) transforms.
// During the 3.2s breach phase that's a per-frame CPU hit that dominates
// the budget. Solution: stacked stroke layers — multiple <text> elements
// with decreasing stroke-width and increasing stroke-opacity approximate
// a Gaussian glow without invoking any filter primitive. ~10× cheaper per
// frame; visually equivalent at portal scale.
//
// Layout (back → front, all inside one SVG so transforms stay in sync):
//   1. Amber halo: 3 stacked strokes (wide / mid / inner)
//   2. Indigo edge: 2 stacked strokes, both breathing in sync
//   3. The black curtain rect, mask-cut by the same ADAPTIVE shape
//   4. CSS scanline overlay sits ABOVE the SVG
//
// Above the SVG, a one-frame muzzle-flash fires at progress = 0.28.
// Curtain opacity lags scale by 15% — the hole blows open first, the
// walls dissolve afterwards.

interface BootCurtainProps {
  progress: number;
}

const VIEWBOX  = 100;
const FONT_VBU = 14;
const HALF     = VIEWBOX / 2;

// Shared text props for every layer — same font, position, weight, baseline.
const TEXT_PROPS = {
  x: HALF,
  y: HALF,
  textAnchor: 'middle' as const,
  dominantBaseline: 'central' as const,
  fontFamily: '"Cabinet Grotesk", "Inter", system-ui, sans-serif',
  fontWeight: 900 as const,
  fontSize: FONT_VBU,
  letterSpacing: '-0.56',
};

export default function BootCurtain({ progress }: BootCurtainProps) {
  const scale = maskScale(progress);
  const transform = `translate(${HALF} ${HALF}) scale(${scale}) translate(${-HALF} ${-HALF})`;

  // Opacity lag: curtain stays at 1.0 until p=0.85, dissolves across the
  // last 15% of progress. CSS transition on opacity smooths the change.
  const curtainOpacity = 1 - clamp01((progress - 0.85) / 0.15);

  // One-frame muzzle-flash. Fires exactly once when progress first
  // crosses the auto-advance trigger.
  const [flashKey, setFlashKey] = useState<number | null>(null);
  useEffect(() => {
    if (progress >= 0.28 && flashKey === null) {
      setFlashKey(performance.now());
    }
  }, [progress, flashKey]);

  return (
    <div
      className="pointer-events-none fixed inset-0 z-40"
      style={{ opacity: curtainOpacity, transition: 'opacity 220ms cubic-bezier(0.65, 0, 0.35, 1)' }}
      aria-hidden="true"
    >
      <svg
        className="h-full w-full"
        viewBox={`0 0 ${VIEWBOX} ${VIEWBOX}`}
        preserveAspectRatio="xMidYMid slice"
      >
        <defs>
          <mask id="portal-mask" maskUnits="userSpaceOnUse">
            <rect x="0" y="0" width={VIEWBOX} height={VIEWBOX} fill="white" />
            <g transform={transform}>
              <text {...TEXT_PROPS} fill="black">ADAPTIVE</text>
            </g>
          </mask>
        </defs>

        {/* Amber halo — three concentric strokes form a soft warm wash.
            The whole group pulses on a 4.7s prime-offset cycle. */}
        <g transform={transform} className="amber-pulse">
          <text {...TEXT_PROPS} fill="none" stroke="#f59e0b" strokeWidth="2.2" strokeOpacity="0.05">
            ADAPTIVE
          </text>
          <text {...TEXT_PROPS} fill="none" stroke="#f59e0b" strokeWidth="1.1" strokeOpacity="0.10">
            ADAPTIVE
          </text>
          <text {...TEXT_PROPS} fill="#f59e0b" fillOpacity="0.06">
            ADAPTIVE
          </text>
        </g>

        {/* Indigo edge glow — outer stroke at low opacity, inner stroke
            with the breathing animation. Both crisp, no filters. */}
        <g transform={transform}>
          <text {...TEXT_PROPS} fill="none" stroke="#6366f1" strokeWidth="0.9" strokeOpacity="0.18">
            ADAPTIVE
          </text>
          <text
            {...TEXT_PROPS}
            fill="none"
            stroke="#6366f1"
            strokeWidth="0.22"
            className="indigo-breathe"
          >
            ADAPTIVE
          </text>
        </g>

        {/* The curtain. Mask cuts the ADAPTIVE shape, revealing the
            stacked glow layers above (and the hero behind the SVG). */}
        <rect
          x="0"
          y="0"
          width={VIEWBOX}
          height={VIEWBOX}
          fill="#050505"
          mask="url(#portal-mask)"
        />
      </svg>

      {/* Scanline overlay — CSS gradient, ~free to render. */}
      <div className="portal-scanlines absolute inset-0" />

      {/* Single-frame muzzle-flash at the ignition moment. */}
      {flashKey !== null && (
        <div
          key={flashKey}
          className="absolute inset-0 bg-white"
          style={{ animation: 'portalFlash 120ms cubic-bezier(0.4, 0, 0.6, 1) forwards' }}
        />
      )}
    </div>
  );
}
