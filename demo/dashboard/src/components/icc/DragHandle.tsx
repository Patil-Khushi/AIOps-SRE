import { GripVertical } from 'lucide-react';

// Native HTML5 drag (draggable on the row) is not keyboard-operable, so
// Alt+Up/Down on the handle is the real accessible reorder path, not
// optional polish.
export function DragHandle({ onKeyReorder }: { onKeyReorder: (dir: 'up' | 'down') => void }) {
  return (
    <button
      type="button"
      aria-label="Drag to reorder, or use Alt+ArrowUp / Alt+ArrowDown"
      className="cursor-grab touch-none text-[var(--icc-fg-faint)] hover:text-[var(--icc-fg-muted)] active:cursor-grabbing"
      onKeyDown={(e) => {
        if (!e.altKey) return;
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          onKeyReorder('up');
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          onKeyReorder('down');
        }
      }}
    >
      <GripVertical className="h-4 w-4" />
    </button>
  );
}
