import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  BellRing,
  Bell,
  Brain,
  BookOpen,
  Network,
  HeartPulse,
  ShieldCheck,
} from 'lucide-react';
import { clsx } from '@/lib/format';

const NAV = [
  { to: '/console',               label: 'Overview',      icon: LayoutDashboard, end: true },
  { to: '/console/alerts',        label: 'Alert Stream',  icon: BellRing,        end: false },
  { to: '/console/notifications', label: 'Notifications', icon: Bell,            end: false },
  { to: '/console/reasoning',     label: 'AI Reasoning',  icon: Brain,           end: false },
  { to: '/console/knowledge',     label: 'Knowledge',     icon: BookOpen,        end: false },
  { to: '/console/topology',      label: 'Topology',      icon: Network,         end: false },
  { to: '/console/health',        label: 'System Health', icon: HeartPulse,      end: false },
];

export default function Sidebar() {
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
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              clsx(
                'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-accent/10 text-accent'
                  : 'text-ink-600 hover:bg-ink-100 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-ink-50',
              )
            }
          >
            <Icon className="h-4 w-4" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-ink-200 p-3 text-[11px] text-ink-500 dark:border-ink-700 dark:text-ink-400">
        <p className="font-mono">v0.1.0 · POC</p>
        <p className="mt-1">Phase 1 — Reactive backbone</p>
      </div>
    </aside>
  );
}
