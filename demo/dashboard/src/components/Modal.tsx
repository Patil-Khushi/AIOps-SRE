import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

// The app had no modal/dialog primitive before this — every existing overlay
// (Approvals.tsx's ActionDropdown, hitl-ui's) is a click-away popover, not a
// real dialog. Portal to document.body, trap focus loosely (Escape + backdrop
// click close it; a real focus trap is a follow-up if this sees more use),
// scroll-lock the body while open.
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  labelledBy,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  labelledBy?: string;
}) {
  const titleId = labelledBy ?? 'modal-title';
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    dialogRef.current?.focus();
    return () => {
      document.body.style.overflow = prevOverflow;
      document.removeEventListener('keydown', onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} aria-hidden />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative z-10 w-full max-w-md rounded-xl border p-5 shadow-2xl outline-none"
        style={{ background: 'var(--icc-surface)', borderColor: 'var(--icc-border)' }}
      >
        <div className="flex items-center justify-between gap-2">
          <h2 id={titleId} className="text-sm font-semibold" style={{ color: 'var(--icc-fg)' }}>
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 transition-colors hover:bg-[var(--icc-surface-2)]"
          >
            <X className="h-4 w-4" style={{ color: 'var(--icc-fg-muted)' }} />
          </button>
        </div>
        <div className="mt-3">{children}</div>
        {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
