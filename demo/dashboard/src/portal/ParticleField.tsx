import { memo, useEffect, useRef } from 'react';

// Deep-space particle field — base layer behind every other boot element.
// 100 stars on independent sine orbits, with a centre-mass distribution
// bias so the eye reads it as a constellation, not noise.
//
// Connections: a thin indigo mesh between stars within 90px, opacity
// falling off with distance.
//
// Scroll-driven: as progress climbs, every star accelerates toward the
// viewport centre (velocity = base × (1 + p × 12)) — being fed into the
// ADAPTIVE wordmark. Above 0.95 the field scatters outward and fades.
//
// Performance notes:
//   - Spatial grid bins particles by CONNECT_DISTANCE so connection
//     checks are O(N) on average instead of O(N²).
//   - Position cached once per frame, not per pair.
//   - DPR capped at 1.5 to keep canvas pixel count manageable on Retina.
//   - React.memo'd because the rAF loop reads progress via a ref, so
//     re-renders triggered by progress changes do nothing useful.

interface ParticleFieldProps {
  progress: number;
}

interface Particle {
  baseX: number;
  baseY: number;
  offsetX: number;
  offsetY: number;
  phaseX: number;
  phaseY: number;
  speedX: number;
  speedY: number;
  amplitudeX: number;
  amplitudeY: number;
  size: number;
  baseOpacity: number;
  anchor: boolean;
}

const PARTICLE_COUNT       = 100;
const ANCHOR_COUNT         = 4;
const CONNECT_DISTANCE     = 90;
const CONNECT_DIST_SQ      = CONNECT_DISTANCE * CONNECT_DISTANCE;
const CONNECT_MAX_OPACITY  = 0.08;
const SCATTER_START        = 0.95;
const BASE_INWARD_PX_PER_S = 4;
const MAX_DPR              = 1.5;

function makeParticles(width: number, height: number): Particle[] {
  const cx = width / 2;
  const cy = height / 2;
  const out: Particle[] = [];
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    const u = Math.random();
    const r = Math.sqrt(u) * Math.min(width, height) * 0.55;
    const theta = Math.random() * Math.PI * 2;
    const anchor = i < ANCHOR_COUNT;
    out.push({
      baseX: cx + Math.cos(theta) * r,
      baseY: cy + Math.sin(theta) * r,
      offsetX: 0,
      offsetY: 0,
      phaseX: Math.random() * Math.PI * 2,
      phaseY: Math.random() * Math.PI * 2,
      speedX: 0.3 + Math.random() * 0.5,
      speedY: 0.3 + Math.random() * 0.5,
      amplitudeX: 6 + Math.random() * 12,
      amplitudeY: 6 + Math.random() * 12,
      size: anchor ? 2.0 : 0.5 + Math.random() * 1.3,
      baseOpacity: anchor ? 0.7 : 0.15 + Math.random() * 0.25,
      anchor,
    });
  }
  return out;
}

function ParticleFieldImpl({ progress }: ParticleFieldProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const particlesRef = useRef<Particle[] | null>(null);
  const progressRef = useRef(progress);

  // Mirror prop to ref in render body — cheaper than a useEffect cycle,
  // and we don't need any side effect, just the latest value visible to
  // the rAF loop on its next tick.
  progressRef.current = progress;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);

    const resize = () => {
      const w = window.innerWidth;
      const h = window.innerHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      particlesRef.current = makeParticles(w, h);
    };
    resize();
    window.addEventListener('resize', resize);

    let lastTime = performance.now();
    let frameId = 0;
    // Reusable per-frame buffers (avoid GC pressure).
    const px: number[] = new Array(PARTICLE_COUNT);
    const py: number[] = new Array(PARTICLE_COUNT);

    const tick = (now: number) => {
      const dt = Math.min((now - lastTime) / 1000, 0.05);
      lastTime = now;

      const w = window.innerWidth;
      const h = window.innerHeight;
      const cx = w / 2;
      const cy = h / 2;
      const p = progressRef.current;

      ctx.clearRect(0, 0, w, h);

      const particles = particlesRef.current;
      if (!particles) {
        frameId = requestAnimationFrame(tick);
        return;
      }

      const inwardVel = BASE_INWARD_PX_PER_S * (1 + p * 12);
      const scatterT  = p > SCATTER_START ? (p - SCATTER_START) / (1 - SCATTER_START) : 0;
      const scatterVel = scatterT * 600;

      // ── Update positions + cache display coords ──────────────────
      for (let i = 0; i < particles.length; i++) {
        const part = particles[i];
        part.phaseX += part.speedX * dt;
        part.phaseY += part.speedY * dt;
        const driftX = Math.sin(part.phaseX) * part.amplitudeX;
        const driftY = Math.sin(part.phaseY) * part.amplitudeY;

        const curX = part.baseX + driftX + part.offsetX;
        const curY = part.baseY + driftY + part.offsetY;
        const dx = cx - curX;
        const dy = cy - curY;
        const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);

        if (scatterT > 0) {
          part.offsetX -= (dx / dist) * scatterVel * dt;
          part.offsetY -= (dy / dist) * scatterVel * dt;
        } else {
          part.offsetX += (dx / dist) * inwardVel * dt;
          part.offsetY += (dy / dist) * inwardVel * dt;
        }

        px[i] = part.baseX + Math.sin(part.phaseX) * part.amplitudeX + part.offsetX;
        py[i] = part.baseY + Math.sin(part.phaseY) * part.amplitudeY + part.offsetY;
      }

      // ── Spatial grid: bin particles into CONNECT_DISTANCE-sized cells.
      //    Drawing connections then only checks within-cell and 8-neighbour
      //    pairs, taking the work from O(N²) to roughly O(N).
      const cellSize = CONNECT_DISTANCE;
      const cols = Math.max(1, Math.ceil(w / cellSize));
      const rows = Math.max(1, Math.ceil(h / cellSize));
      const grid: number[][] = new Array(cols * rows);
      for (let g = 0; g < grid.length; g++) grid[g] = [];
      for (let i = 0; i < particles.length; i++) {
        const cxi = Math.max(0, Math.min(cols - 1, Math.floor(px[i] / cellSize)));
        const cyi = Math.max(0, Math.min(rows - 1, Math.floor(py[i] / cellSize)));
        grid[cyi * cols + cxi].push(i);
      }

      // ── Draw connections ─────────────────────────────────────────
      ctx.lineWidth = 0.5;
      const fadeOut = 1 - scatterT;
      for (let i = 0; i < particles.length; i++) {
        const cxi = Math.max(0, Math.min(cols - 1, Math.floor(px[i] / cellSize)));
        const cyi = Math.max(0, Math.min(rows - 1, Math.floor(py[i] / cellSize)));
        for (let ox = -1; ox <= 1; ox++) {
          const ncx = cxi + ox;
          if (ncx < 0 || ncx >= cols) continue;
          for (let oy = -1; oy <= 1; oy++) {
            const ncy = cyi + oy;
            if (ncy < 0 || ncy >= rows) continue;
            const cell = grid[ncy * cols + ncx];
            for (let k = 0; k < cell.length; k++) {
              const j = cell[k];
              if (j <= i) continue; // dedup pairs
              const ddx = px[i] - px[j];
              const ddy = py[i] - py[j];
              const d2 = ddx * ddx + ddy * ddy;
              if (d2 > CONNECT_DIST_SQ) continue;
              const d = Math.sqrt(d2);
              const alpha = (1 - d / CONNECT_DISTANCE) * CONNECT_MAX_OPACITY * fadeOut;
              if (alpha <= 0.005) continue;
              ctx.strokeStyle = `rgba(99, 102, 241, ${alpha})`;
              ctx.beginPath();
              ctx.moveTo(px[i], py[i]);
              ctx.lineTo(px[j], py[j]);
              ctx.stroke();
            }
          }
        }
      }

      // ── Draw particles ───────────────────────────────────────────
      for (let i = 0; i < particles.length; i++) {
        const part = particles[i];
        ctx.fillStyle = `rgba(255, 255, 255, ${part.baseOpacity * fadeOut})`;
        ctx.beginPath();
        ctx.arc(px[i], py[i], part.size, 0, Math.PI * 2);
        ctx.fill();
        if (part.anchor) {
          ctx.fillStyle = `rgba(99, 102, 241, ${0.20 * fadeOut})`;
          ctx.beginPath();
          ctx.arc(px[i], py[i], part.size * 3, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      frameId = requestAnimationFrame(tick);
    };
    frameId = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(frameId);
      window.removeEventListener('resize', resize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 z-10"
      aria-hidden="true"
    />
  );
}

// Memoise: the canvas runs its own rAF loop and reads progress via ref, so
// React re-renders triggered by every progress tick do no useful work.
const ParticleField = memo(ParticleFieldImpl, () => true);
export default ParticleField;
