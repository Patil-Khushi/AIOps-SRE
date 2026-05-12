import { clsx } from '@/lib/format';
import type { Severity, Status } from '@/types/api';

const SEV_STYLES: Record<Severity, string> = {
  'Sev-1': 'bg-sev1/15 text-sev1 border-sev1/40',
  'Sev-2': 'bg-sev2/15 text-sev2 border-sev2/40',
  'Sev-3': 'bg-sev3/15 text-sev3 border-sev3/40',
  'Sev-4': 'bg-sev4/15 text-sev4 border-sev4/40',
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-bold uppercase tracking-wider',
        SEV_STYLES[severity],
      )}
    >
      {severity}
    </span>
  );
}

export function StatusChip({ status }: { status: Status }) {
  const styles =
    status === 'Active'
      ? 'bg-ok/15 text-ok border-ok/40'
      : 'bg-warn/15 text-warn border-warn/40';
  return (
    <span className={clsx('inline-flex rounded-full border px-2 py-0.5 text-[11px] font-medium', styles)}>
      {status}
    </span>
  );
}
