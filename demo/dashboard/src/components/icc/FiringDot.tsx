// A pulsing ring dot for "currently firing" — distinct from Tailwind's stock
// animate-pulse (opacity-only); the design spec wants a ring that expands.
export function FiringDot({ firing, label }: { firing: boolean; label?: string }) {
  if (!firing) {
    return (
      <span
        className="inline-block h-2 w-2 rounded-full bg-[var(--icc-fg-faint)]"
        title={label ?? 'Not currently firing'}
      />
    );
  }
  return (
    <span className="relative inline-flex h-2 w-2" title={label ?? 'Firing'}>
      <span className="icc-firing-ping absolute inline-flex h-full w-full rounded-full bg-[var(--icc-bad)] opacity-60" />
      <span className="relative inline-flex h-2 w-2 rounded-full bg-[var(--icc-bad)]" />
    </span>
  );
}
