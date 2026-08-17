import { useRef, useState } from 'react';
import type { IncidentRowVM } from '@/lib/incidentVm';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { IncidentRowItem } from './IncidentRowItem';

export function IncidentTable({
  rows,
  order,
  onReorder,
  selectedId,
  checkedIds,
  onSelect,
  onToggleCheck,
  onDebug,
  onOpenWorkspace,
  loading,
  error,
}: {
  rows: IncidentRowVM[];
  order: string[];
  onReorder: (order: string[]) => void;
  selectedId: string | null;
  checkedIds: Set<string>;
  onSelect: (id: string) => void;
  onToggleCheck: (id: string) => void;
  onDebug: (row: IncidentRowVM) => void;
  onOpenWorkspace: (row: IncidentRowVM) => void;
  loading: boolean;
  error: string | null;
}) {
  const dragFrom = useRef<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);

  const byId = new Map(rows.map((r) => [r.id, r]));
  const ordered = order.map((id) => byId.get(id)).filter((r): r is IncidentRowVM => !!r);

  const move = (id: string, dir: 'up' | 'down') => {
    const idx = order.indexOf(id);
    const swapWith = dir === 'up' ? idx - 1 : idx + 1;
    if (idx < 0 || swapWith < 0 || swapWith >= order.length) return;
    const next = [...order];
    [next[idx], next[swapWith]] = [next[swapWith], next[idx]];
    onReorder(next);
  };

  const reorderByDrag = (targetId: string) => {
    const from = dragFrom.current;
    setDragOverId(null);
    if (!from || from === targetId) return;
    const next = order.filter((id) => id !== from);
    const targetIdx = next.indexOf(targetId);
    next.splice(targetIdx, 0, from);
    onReorder(next);
    dragFrom.current = null;
  };

  if (loading && rows.length === 0) return <div className="card"><LoadingState label="Loading incidents…" /></div>;
  if (error) return <div className="card"><ErrorState error={error} /></div>;
  if (ordered.length === 0) {
    return (
      <div className="card">
        <EmptyState label="No incidents" hint="Inject a scenario to see it appear here, triaged and ready for RCA." />
      </div>
    );
  }

  return (
    <ul className="card overflow-hidden !p-0" role="list">
      {ordered.map((row) => (
        <div
          key={row.id}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOverId(row.id);
          }}
          className={dragOverId === row.id ? 'outline outline-2 outline-[var(--icc-accent)]' : undefined}
        >
          <IncidentRowItem
            row={row}
            selected={selectedId === row.id}
            checked={checkedIds.has(row.id)}
            draggable
            onSelect={() => onSelect(row.id)}
            onToggleCheck={() => onToggleCheck(row.id)}
            onDebug={() => onDebug(row)}
            onOpenWorkspace={() => onOpenWorkspace(row)}
            onKeyReorder={(dir) => move(row.id, dir)}
            onDragStart={() => {
              dragFrom.current = row.id;
            }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => reorderByDrag(row.id)}
          />
        </div>
      ))}
    </ul>
  );
}
