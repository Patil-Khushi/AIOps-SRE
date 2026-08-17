import type { ReactNode } from 'react';
import { useTheme } from '@/hooks/useTheme';

// The Incident Command Center's CSS-variable theme scope (src/styles/theme.css).
// `data-theme` here mirrors the same attribute the theme store already sets on
// <html> — deliberate redundancy, not a second source of truth: both come from
// the one module-level store in useTheme.ts, so they cannot disagree, and it
// makes the ICC subtree self-contained (paints its own background over the
// app's `body` classes, and is easy to reason about in isolation).
export function IccRoot({ children, className }: { children: ReactNode; className?: string }) {
  const { theme } = useTheme();
  return (
    <div className={['icc-root', className].filter(Boolean).join(' ')} data-theme={theme}>
      {children}
    </div>
  );
}
