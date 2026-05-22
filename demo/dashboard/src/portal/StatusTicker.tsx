import { useEffect, useState } from 'react';

// Live status ticker — typewriter-cycles through five lines so the boot
// state reads as alive, not a screensaver. The last line stays on-screen
// indefinitely once typed; it's the action prompt the user needs to see.
//
// All timings are in CSS-units (ms) so the rhythm tunes via constants.

const LINES = [
  'SYSTEM · ADAPTIVE AIOPS v2.0 · INITIALISING',
  'LOADING 30 AGENT DEFINITIONS ·········',
  'PHASE REGISTRY · OK',
  'AGENTIC RUNTIME · STANDBY',
  'SCROLL OR CLICK TO BOOT',
];
const CHAR_MS = 40;
const HOLD_MS = 600;

export default function StatusTicker() {
  const [lineIdx, setLineIdx] = useState(0);
  const [charIdx, setCharIdx] = useState(0);

  useEffect(() => {
    const line = LINES[lineIdx];

    // Still typing this line?
    if (charIdx < line.length) {
      const t = setTimeout(() => setCharIdx((c) => c + 1), CHAR_MS);
      return () => clearTimeout(t);
    }

    // Line complete. Hold, then advance to the next — but stop on the
    // last line so the prompt stays visible until the user reacts.
    if (lineIdx < LINES.length - 1) {
      const t = setTimeout(() => {
        setLineIdx((i) => i + 1);
        setCharIdx(0);
      }, HOLD_MS);
      return () => clearTimeout(t);
    }
  }, [lineIdx, charIdx]);

  const display = LINES[lineIdx].slice(0, charIdx);
  const onFinalLine = lineIdx === LINES.length - 1 && charIdx >= LINES[lineIdx].length;

  return (
    <p
      className="font-mono text-[10px] uppercase text-white/60"
      style={{ letterSpacing: '0.25em', minHeight: '1.2em' }}
    >
      {display}
      <span className={onFinalLine ? 'typewriter-caret ml-1' : 'ml-1'}>▎</span>
    </p>
  );
}
