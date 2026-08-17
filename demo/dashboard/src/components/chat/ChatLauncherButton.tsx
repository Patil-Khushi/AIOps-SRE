import { MessageCircle, X } from 'lucide-react';
import { useChatDock } from './ChatDockProvider';

// The familiar bottom-right bubble every website chat widget has. Only shown
// once there's an incident to talk about (dock.row) — this app's chat is
// always incident-scoped, so a bubble with nothing behind it would open onto
// an empty panel. Doubles as the close button while the panel is open,
// swapping icon + color rather than disappearing, so its position never
// jumps.
export function ChatLauncherButton() {
  const dock = useChatDock();
  if (!dock.row) return null;

  return (
    <button
      type="button"
      onClick={dock.toggle}
      aria-label={dock.open ? 'Close RCA agent' : 'Open RCA agent'}
      className={
        'fixed bottom-6 right-6 z-50 flex h-14 w-14 items-center justify-center rounded-full ' +
        'shadow-lg transition-all duration-300 ease-out hover:scale-105 ' +
        (dock.open
          ? 'bg-[var(--icc-surface-2)] text-[var(--icc-fg-muted)]'
          : 'bg-[var(--icc-accent)] text-white')
      }
    >
      {dock.open ? <X className="h-6 w-6" /> : <MessageCircle className="h-6 w-6" />}
    </button>
  );
}
