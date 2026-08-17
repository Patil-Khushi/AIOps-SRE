import { useEffect, useState } from 'react';

// Ported verbatim from demo/hitl-ui/src/App.tsx — the approval-expiry
// countdown used there has no equivalent in demo/dashboard today.
export function useCountdown(isoTarget: string): string {
  const target = new Date(isoTarget).getTime();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const remain = Math.max(0, Math.round((target - now) / 1000));
  if (remain <= 0) return 'expired';
  const m = Math.floor(remain / 60);
  const s = remain % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}
