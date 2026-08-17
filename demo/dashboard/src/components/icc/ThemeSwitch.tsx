import { Sun, Moon } from 'lucide-react';
import { useTheme, type Theme } from '@/hooks/useTheme';

// Segmented Light/Dark switch for the ICC. Reads/writes the SAME store as the
// existing Header sun/moon toggle (useTheme.ts) — clicking either updates both
// instantly, because there is only one theme value in the app.
export function ThemeSwitch({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();

  const option = (value: Theme, Icon: typeof Sun, label: string) => {
    const active = theme === value;
    return (
      <button
        type="button"
        role="radio"
        aria-checked={active}
        aria-label={label}
        onClick={() => setTheme(value)}
        className={[
          'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
          active
            ? 'bg-[var(--icc-accent)] text-white'
            : 'text-[var(--icc-fg-muted)] hover:text-[var(--icc-fg)]',
        ].join(' ')}
      >
        <Icon className="h-3.5 w-3.5" />
        {label}
      </button>
    );
  };

  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className={[
        'inline-flex items-center gap-0.5 rounded-lg border p-0.5',
        'border-[var(--icc-border)] bg-[var(--icc-surface-2)]',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {option('light', Sun, 'Light')}
      {option('dark', Moon, 'Dark')}
    </div>
  );
}
