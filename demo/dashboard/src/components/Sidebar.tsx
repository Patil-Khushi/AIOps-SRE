import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  BellRing,
  Bell,
  Brain,
  Gavel,
  Network,
  HeartPulse,
  ShieldCheck,
} from 'lucide-react';
import { clsx } from '@/lib/format';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';

const NAV = [
  { to: '/console',               label: 'Overview',      icon: LayoutDashboard, end: true,  external: false },
  { to: '/console/alerts',        label: 'Alert Stream',  icon: BellRing,        end: false, external: false },
  // Approvals routes straight to the standalone HITL approver console (/hitl),
  // which both lists and acts on requests — no separate dashboard page needed.
  { to: '/hitl',                  label: 'Approvals',     icon: Gavel,           end: false, external: true },
  { to: '/console/notifications', label: 'Notifications', icon: Bell,            end: false, external: false },
  { to: '/console/reasoning',     label: 'AI Reasoning',  icon: Brain,           end: false, external: false },
  { to: '/console/topology',      label: 'Topology',      icon: Network,         end: false, external: false },
  { to: '/console/health',        label: 'System Health', icon: HeartPulse,      end: false, external: false },
];

export default function Sidebar() {
  // Live count of pending HITL approvals — drives the nav badge.
  const { data: approvals } = useFetch(() => api.approvals(false), { intervalMs: 5000 });
  const pending = approvals?.count ?? 0;

  return (
    <aside className="hidden w-64 flex-shrink-0 border-r border-ink-200 bg-white dark:border-ink-700 dark:bg-ink-900 md:flex md:flex-col">
      <div className="flex h-16 items-center gap-2 border-b border-ink-200 px-5 dark:border-ink-700">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/15">
          <ShieldCheck className="h-5 w-5 text-accent" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">Adaptive AIOps</span>
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
            RA-001 · Alert Triage
          </span>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 p-3">
        {NAV.map(({ to, label, icon: Icon, end, external }) => {
          const base =
            'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors';
          const inactive =
            'text-ink-600 hover:bg-ink-100 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-ink-50';
          const badge =
            to === '/hitl' && pending > 0 ? (
              <span className="ml-auto inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-warn px-1.5 text-[11px] font-bold text-white">
                {pending}
              </span>
            ) : null;

          // The HITL approver console is a separate app — plain anchor, not a router link.
          if (external) {
            return (
              <a key={to} href={to} className={clsx(base, inactive)}>
                <Icon className="h-4 w-4" />
                <span>{label}</span>
                {badge}
              </a>
            );
          }

          return (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(base, isActive ? 'bg-accent/10 text-accent' : inactive)
              }
            >
              <Icon className="h-4 w-4" />
              <span>{label}</span>
              {badge}
            </NavLink>
          );
        })}
      </nav>

      <div className="border-t border-ink-200 p-3 text-[11px] text-ink-500 dark:border-ink-700 dark:text-ink-400">
        <p className="font-mono">v0.1.0 · POC</p>
        <p className="mt-1">Phase 1 — Reactive backbone</p>
      </div>
    </aside>
  );
}
