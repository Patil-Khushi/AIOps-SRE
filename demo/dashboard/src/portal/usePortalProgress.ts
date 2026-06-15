import { useCallback, useEffect, useRef, useState } from 'react';
import { clamp01, easeInOutCubic } from './easing';

// Virtual-scroll boot-curtain progress.
//
// The landing page itself does not scroll — wheel/touch/keyboard input
// accumulates into a 0→1 "boot progress" value. Once progress crosses 15%
// the platform "confirms boot" by auto-advancing the rest of the way over
// ~3.5s with an ease-in-out cubic curve.
//
// Inputs are accepted from THREE places to dodge browser quirks:
//   1. Keyboard/touch listeners on `window` (always works)
//   2. The React-level `bindWheel` callback returned by this hook, which
//      callers attach via <div onWheel={bindWheel}> on the page root —
//      bypasses the compositor-level wheel suppression that affects
//      overflow-locked viewports in Chrome.
//   3. A document-level wheel listener (belt + suspenders).
//
// Returned `setProgress` lets callers jump (skip-intro click).

// v2 timing: a normal mouse-wheel notch (deltaY ≈ 100) yields 0.10 progress,
// so the user clears the resistance band (0→0.15) over ~2 notches and trips
// the auto-advance trigger at 0.28 on the third — letting the entire three-
// act arc (resistance → flash → breach) play out visibly. Previously 1/200
// gave 0.5/notch, slamming users straight into breach.
const WHEEL_SENSITIVITY = 1 / 1000;
const TOUCH_SENSITIVITY = 1 / 600;
// 28% is the break-point / "ignition confirmed" moment the muzzle-flash
// hooks into. Breach phase runs ~3.2s.
const AUTO_ADVANCE_AT   = 0.28;
const AUTO_ADVANCE_MS   = 3200;

export interface PortalProgress {
  progress: number;
  setProgress: (p: number) => void;
  autoAdvancing: boolean;
  /** Total wheel events received since mount — for debug overlays. */
  wheelEventCount: number;
  /** Attach to the page root: <div onWheel={onWheel}>. */
  onWheel: (e: React.WheelEvent | WheelEvent) => void;
}

export function usePortalProgress(initial = 0): PortalProgress {
  const [progress, setProgressState] = useState(initial);
  const [wheelEventCount, setWheelEventCount] = useState(0);
  const progressRef = useRef(initial);
  const autoAdvancingRef = useRef(false);
  const [autoAdvancing, setAutoAdvancing] = useState(false);

  const setProgress = useCallback((p: number) => {
    const next = clamp01(p);
    progressRef.current = next;
    setProgressState(next);
  }, []);

  const bump = useCallback((delta: number) => {
    if (autoAdvancingRef.current) return;
    setProgress(progressRef.current + delta);
  }, [setProgress]);

  // Shared wheel handler — used by both the React `onWheel` prop and the
  // document-level addEventListener. Dedup is implicit: the event hits one
  // path or the other (or both); doubling up just speeds up the boot, which
  // is the desired direction.
  const onWheel = useCallback((e: React.WheelEvent | WheelEvent) => {
    if (progressRef.current >= 1) return;
    setWheelEventCount((c) => c + 1);
    bump(e.deltaY * WHEEL_SENSITIVITY);
  }, [bump]);

  // Document-level wheel + window-level touch/keyboard.
  useEffect(() => {
    const docOnWheel = (e: WheelEvent) => {
      if (progressRef.current >= 1) return;
      onWheel(e);
    };

    let touchY: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (touchY == null || progressRef.current >= 1) return;
      const y = e.touches[0]?.clientY ?? touchY;
      const dy = touchY - y;
      touchY = y;
      bump(dy * TOUCH_SENSITIVITY);
    };
    const onTouchEnd = () => { touchY = null; };

    const onKey = (e: KeyboardEvent) => {
      if (progressRef.current >= 1) return;
      if (e.key === 'ArrowDown' || e.key === 'PageDown' || e.key === ' ') {
        e.preventDefault();
        bump(0.25);
      } else if (e.key === 'ArrowUp' || e.key === 'PageUp') {
        e.preventDefault();
        bump(-0.25);
      } else if (e.key === 'Escape' || e.key === 'Enter') {
        e.preventDefault();
        setProgress(1);
      }
    };

    document.addEventListener('wheel', docOnWheel, { passive: true });
    window.addEventListener('touchstart', onTouchStart, { passive: true });
    window.addEventListener('touchmove', onTouchMove, { passive: true });
    window.addEventListener('touchend', onTouchEnd, { passive: true });
    window.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('wheel', docOnWheel);
      window.removeEventListener('touchstart', onTouchStart);
      window.removeEventListener('touchmove', onTouchMove);
      window.removeEventListener('touchend', onTouchEnd);
      window.removeEventListener('keydown', onKey);
    };
  }, [bump, onWheel, setProgress]);

  // Auto-advance once the user signals intent past 15%.
  //
  // Two-stage to avoid a self-cancelling effect: the first effect watches
  // `progress` and flips a one-shot trigger; the second effect runs the
  // rAF animation and depends ONLY on that trigger. If `progress` were a
  // dep of the animation effect, every setProgress() call from inside the
  // tick would re-run the effect, run the cleanup, and cancelAnimationFrame
  // its own next tick — freezing the boot at whatever value first crossed
  // the threshold (we hit this in dev: stuck-at-25% bug, 2026-05-22).
  const [autoAdvanceStarted, setAutoAdvanceStarted] = useState(false);

  useEffect(() => {
    // Don't kick off the auto-advance when we start already-booted (initial=1,
    // i.e. a returning visitor skipping the intro) — only on a real ascent.
    if (progress >= AUTO_ADVANCE_AT && progress < 1 && !autoAdvanceStarted) {
      setAutoAdvanceStarted(true);
    }
  }, [progress, autoAdvanceStarted]);

  useEffect(() => {
    if (!autoAdvanceStarted) return;
    autoAdvancingRef.current = true;
    setAutoAdvancing(true);

    const startProgress = progressRef.current;
    const startTime = performance.now();
    let frameId = 0;

    const tick = (now: number) => {
      const t = (now - startTime) / AUTO_ADVANCE_MS;
      if (t >= 1) {
        setProgress(1);
        return;
      }
      const eased = easeInOutCubic(t);
      setProgress(startProgress + (1 - startProgress) * eased);
      frameId = requestAnimationFrame(tick);
    };
    frameId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameId);
  }, [autoAdvanceStarted, setProgress]);

  return { progress, setProgress, autoAdvancing, wheelEventCount, onWheel };
}
