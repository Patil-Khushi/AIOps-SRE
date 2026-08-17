import { useSyncExternalStore } from 'react';

// ─── The single theme store ──────────────────────────────────────────────
//
// Was previously per-component useState — Header.tsx, BrowseLayout.tsx, and
// Topology.tsx each held their own copy, so toggling in one didn't update
// the others (reactflow/recharts colors in Topology went stale until a
// remount). Now there is exactly one module-level value, and every consumer
// (including the new data-theme-driven CSS variable system in
// src/styles/theme.css) reads and writes through this same store — a second,
// independently-toggled theme is structurally impossible.

export type Theme = 'light' | 'dark';
const STORAGE_KEY = 'aiops-theme';

const listeners = new Set<() => void>();

function readStored(): Theme | null {
  if (typeof window === 'undefined') return null;
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === 'light' || stored === 'dark' ? stored : null;
}

function prefersDark(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-color-scheme: dark)').matches;
}

let current: Theme = readStored() ?? (prefersDark() ? 'dark' : 'light');

// The one place the .dark class, colorScheme, and data-theme attribute are
// painted — so they can never disagree with each other. data-theme is what
// src/styles/theme.css's `--icc-*` variables key off; the existing Tailwind
// `dark:` classes keep working unchanged off the same class toggle.
function paint(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.toggle('dark', theme === 'dark');
  root.style.colorScheme = theme;
  root.dataset.theme = theme;
}
paint(current);

function notify(): void {
  listeners.forEach((l) => l());
}

// An explicit user choice (the Header toggle or the segmented ThemeSwitch) —
// persists, so it wins over the OS preference for the rest of this browser.
function setThemeInternal(theme: Theme): void {
  if (theme === current) return;
  current = theme;
  paint(current);
  localStorage.setItem(STORAGE_KEY, theme);
  notify();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): Theme {
  return current;
}

// Follow the OS preference live as long as the user hasn't made an explicit
// choice yet (no stored value). Previously the preference was read once at
// boot and then ignored for the rest of the session.
if (typeof window !== 'undefined' && window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
    if (readStored() !== null) return; // an explicit choice already exists
    current = e.matches ? 'dark' : 'light';
    paint(current);
    notify();
  });
}

export function useTheme(): { theme: Theme; toggle: () => void; setTheme: (t: Theme) => void } {
  const theme = useSyncExternalStore(subscribe, getSnapshot);
  return {
    theme,
    toggle: () => setThemeInternal(theme === 'dark' ? 'light' : 'dark'),
    setTheme: setThemeInternal,
  };
}
