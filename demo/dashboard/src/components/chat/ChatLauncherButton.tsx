import { MessageCircle } from 'lucide-react';
import { useChatDock } from './ChatDockProvider';

// The familiar bottom-right bubble every website chat widget has. Only shown
// once there's an incident to talk about (dock.row) — this app's chat is
// always incident-scoped, so a bubble with nothing behind it would open onto
// an empty panel. Fades out while the panel is open instead of swapping to an
// X: the panel's own header close button is the one close affordance, so the
// two never fight for the same corner of the screen.
export function ChatLauncherButton() {
  const dock = useChatDock();
  if (!dock.row) return null;

  return (
    <button
      type="button"
      onClick={dock.toggle}
      aria-label="Open RCA agent"
      aria-hidden={dock.open}
      tabIndex={dock.open ? -1 : 0}
      className={
        'fixed bottom-6 right-6 z-50 flex h-14 w-14 items-center justify-center rounded-full ' +
        'bg-[var(--icc-accent)] text-white shadow-lg shadow-[var(--icc-accent)]/30 ' +
        'transition-all duration-300 ease-out hover:scale-105 ' +
        (dock.open ? 'pointer-events-none scale-75 opacity-0' : 'scale-100 opacity-100')
      }
    >
      {/* Soft pulsing ring so the entry point reads as "available" on a page
          that otherwise has no chat affordance in view. */}
      {!dock.open && (
        <span className="icc-firing-ping pointer-events-none absolute inset-0 rounded-full bg-[var(--icc-accent)]" />
      )}
      <MessageCircle className="relative h-6 w-6" />
    </button>
  );
}
