import { useEffect, useRef } from 'react';
import { Send } from 'lucide-react';

// How tall the composer is allowed to grow before it starts scrolling
// internally instead — chosen so a long auto-typed prompt (the common case
// this exists for) is fully visible without eating the whole dock height.
const MAX_HEIGHT_PX = 160;

export function ChatComposer({
  value,
  onChange,
  onSubmit,
  disabled,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grow to fit content instead of clipping it behind a scrollbar in a
  // fixed 2-row box — re-measured on every value change (typing, auto-type,
  // or a slash-command chip filling the field programmatically).
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [value]);

  return (
    <div className="border-t border-[var(--icc-border)] p-3">
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              onSubmit();
            }
          }}
          placeholder={placeholder}
          rows={2}
          disabled={disabled}
          style={{ maxHeight: MAX_HEIGHT_PX }}
          className="input flex-1 resize-none overflow-y-auto !bg-[var(--icc-surface-2)]"
        />
        <button
          type="button"
          onClick={onSubmit}
          disabled={disabled || !value.trim()}
          className="btn btn-primary !py-2"
          aria-label="Send"
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
      <p className="mt-1.5 text-center text-[10px] text-[var(--icc-fg-faint)]">
        ↵ to send · answers cite the investigation record
      </p>
    </div>
  );
}
