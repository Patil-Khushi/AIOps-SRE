// Easing helpers for the boot-curtain portal — never linear.
// Every transition uses a weighted cubic so the system feels "heavy" coming
// online, the way production systems read on Apple / NASA product pages.

export const clamp01 = (n: number): number =>
  n < 0 ? 0 : n > 1 ? 1 : n;

// Resistance — the slow, dragged-back feel during scroll 0–15%.
//   cubic-bezier(0.25, 0, 0.5, 0) — basically a powered-cubic that takes
//   a long time to ramp.
export function easeResistance(t: number): number {
  return cubicBezier(0.25, 0, 0.5, 0, clamp01(t));
}

// Breach — once auto-advance fires, the scale rockets.
//   cubic-bezier(0.77, 0, 0.175, 1).
export function easeBreach(t: number): number {
  return cubicBezier(0.77, 0, 0.175, 1, clamp01(t));
}

// Plain ease-in-out cubic — used by usePortalProgress's auto-advance ramp.
export function easeInOutCubic(t: number): number {
  const x = clamp01(t);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

// Piecewise scale for the mask zoom — three phases that feel kinetically
// distinct: heavy resistance, brief continuation through the flash window,
// then the breach detonation.
//   0.00 → 0.15  : 1×  → 3×    (resistance)
//   0.15 → 0.30  : 3×  → 4×    (continuation through flash)
//   0.30 → 1.00  : 4×  → 220×  (breach)
export function maskScale(progress: number): number {
  const p = clamp01(progress);
  if (p <= 0.15) {
    return 1 + 2 * easeResistance(p / 0.15);
  }
  if (p <= 0.30) {
    return 3 + 1 * easeResistance((p - 0.15) / 0.15);
  }
  return 4 + 216 * easeBreach((p - 0.30) / 0.70);
}

// Closed-form approximation of cubic-bezier(p1x, p1y, p2x, p2y).
// Adapted from the WebKit timing-function implementation.
function cubicBezier(
  p1x: number,
  p1y: number,
  p2x: number,
  p2y: number,
  t: number,
): number {
  const cx = 3 * p1x;
  const bx = 3 * (p2x - p1x) - cx;
  const ax = 1 - cx - bx;

  const cy = 3 * p1y;
  const by = 3 * (p2y - p1y) - cy;
  const ay = 1 - cy - by;

  let u = t;
  for (let i = 0; i < 8; i++) {
    const x = ((ax * u + bx) * u + cx) * u - t;
    if (Math.abs(x) < 1e-6) break;
    const dx = (3 * ax * u + 2 * bx) * u + cx;
    if (Math.abs(dx) < 1e-6) break;
    u -= x / dx;
  }
  return ((ay * u + by) * u + cy) * u;
}
