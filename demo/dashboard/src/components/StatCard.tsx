import { ReactNode } from 'react';
import { clsx } from '@/lib/format';

interface Props {
  label: string;
  value: string | number;
  hint?: string;
  trend?: number | null;          // -ve = good for error metrics; +ve = up
  trendInverse?: boolean;         // true means lower=better (e.g., MTTR)
  icon?: ReactNode;
  intent?: 'default' | 'ok' | 'warn' | 'bad';
}

const INTENT_BORDER: Record<NonNullable<Props['intent']>, string> = {
  default: 'border-ink-200 dark:border-ink-700',
  ok:      'border-ok/40',
  warn:    'border-warn/40',
  bad:     'border-bad/40',
};

const INTENT_VALUE: Record<NonNullable<Props['intent']>, string> = {
  default: 'text-ink-900 dark:text-ink-50',
  ok:      'text-ok',
  warn:    'text-warn',
  bad:     'text-bad',
};

export default function StatCard({ label, value, hint, trend, trendInverse, icon, intent = 'default' }: Props) {
  let trendNode: ReactNode = null;
  if (typeof trend === 'number') {
    const good = trendInverse ? trend < 0 : trend > 0;
    const sign = trend >= 0 ? '+' : '';
    trendNode = (
      <span className={clsx('text-xs font-mono font-semibold', good ? 'text-ok' : 'text-bad')}>
        {sign}{trend.toFixed(1)}%
      </span>
    );
  }
  return (
    <div className={clsx('card animate-slide-up', INTENT_BORDER[intent])}>
      <div className="card-body">
        <div className="flex items-center justify-between">
          <span className="card-title text-[11px]">{label}</span>
          {icon && <span className="text-ink-400 dark:text-ink-500">{icon}</span>}
        </div>
        <div className="mt-2 flex items-baseline gap-2">
          <span className={clsx('text-3xl font-semibold tracking-tight', INTENT_VALUE[intent])}>
            {value}
          </span>
          {trendNode}
        </div>
        {hint && (
          <p className="mt-1 text-xs text-ink-500 dark:text-ink-400">{hint}</p>
        )}
      </div>
    </div>
  );
}
