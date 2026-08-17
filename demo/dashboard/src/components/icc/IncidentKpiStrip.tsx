import { AlertTriangle, ShieldAlert, Siren, Radio } from 'lucide-react';
import StatCard from '@/components/StatCard';
import type { IncidentRowVM } from '@/lib/incidentVm';

export function IncidentKpiStrip({
  rows,
  pendingApprovals,
}: {
  rows: IncidentRowVM[];
  pendingApprovals: number | null;
}) {
  const open = rows.length;
  const sev12 = rows.filter((r) => r.severity === 'Sev-1' || r.severity === 'Sev-2').length;
  const firing = rows.filter((r) => r.firing).length;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatCard label="Open incidents" value={open} icon={<AlertTriangle className="h-4 w-4" />} />
      <StatCard
        label="Sev-1 / Sev-2"
        value={sev12}
        intent={sev12 > 0 ? 'bad' : 'default'}
        icon={<Siren className="h-4 w-4" />}
      />
      <StatCard
        label="Firing now"
        value={firing}
        intent={firing > 0 ? 'warn' : 'ok'}
        icon={<Radio className="h-4 w-4" />}
      />
      <StatCard
        label="Awaiting approval"
        value={pendingApprovals ?? '—'}
        hint={pendingApprovals === null ? 'Not available' : undefined}
        intent={pendingApprovals ? 'warn' : 'default'}
        icon={<ShieldAlert className="h-4 w-4" />}
      />
    </div>
  );
}
