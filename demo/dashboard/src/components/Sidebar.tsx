import type { ComponentType } from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  BellRing,
  Bell,
  Brain,
  Sparkles,
  Siren,
  Gavel,
  Network,
  HeartPulse,
  ShieldCheck,
  BookOpen,
  Users,
} from 'lucide-react';
import { clsx } from '@/lib/format';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { getConsoleAgentId } from '@/lib/consoleScope';
import { getAgentById } from '@/data/agentCatalog';

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  end: boolean;
}

// Every console surface, keyed by route. Each agent's nav is assembled from a
// subset of these so agents stay independent — Alert Triage never shows RCA's
// surface, RCA never shows Alert Triage's stream, etc.
const ITEMS: Record<string, NavItem> = {
  '/console':               { to: '/console',               label: 'Overview',      icon: LayoutDashboard, end: true  },
  '/console/alerts':        { to: '/console/alerts',        label: 'Alert Stream',  icon: BellRing,        end: false },
  '/console/reasoning':     { to: '/console/reasoning',     label: 'AI Reasoning',  icon: Brain,           end: false },
  '/console/rca':           { to: '/console/rca',           label: 'RCA Agent',     icon: Sparkles,        end: false },
  '/console/incident-commander': { to: '/console/incident-commander', label: 'Incident Commander', icon: Siren, end: false },
  '/console/war-room':      { to: '/console/war-room',      label: 'War Room',      icon: Users,           end: false },
  '/console/notifications': { to: '/console/notifications', label: 'Notifications', icon: Bell,            end: false },
  '/console/knowledge':     { to: '/console/knowledge',     label: 'Knowledge',     icon: BookOpen,        end: false },
  '/console/approvals':     { to: '/console/approvals',     label: 'Approvals',     icon: Gavel,           end: false },
  '/console/topology':      { to: '/console/topology',      label: 'Topology',      icon: Network,         end: false },
  '/console/health':        { to: '/console/health',        label: 'System Health', icon: HeartPulse,      end: false },
};

// Shared infra surfaces every agent's console carries.
const SHARED_TAIL = ['/console/topology', '/console/health'];

// Per-agent console surfaces (by agent id). The agent the console was opened
// for (see consoleScope) decides which of these renders. Agents without an
// entry fall back to DEFAULT_SURFACES.
const AGENT_SURFACES: Record<string, string[]> = {
  'alert-triage':        ['/console', '/console/alerts', '/console/reasoning', ...SHARED_TAIL],
  // RCA's console is focused on the analysis + approvals — no Overview/Topology.
  'rca-agent':           ['/console/rca', '/console/health'],
  // Incident Commander coordinates from the alert; its console + the approvals
  // it hands off to are the surface that matters.
  'incident-commander':  ['/console/incident-commander', '/console/approvals', '/console/health'],
  // War-Room Assembler opens straight to its console; carry the alert stream it
  // assembled from plus the shared infra surfaces.
  'war-room-assembler':  ['/console', '/console/alerts', '/console/war-room', ...SHARED_TAIL],
  'notification-router': ['/console', '/console/notifications', ...SHARED_TAIL],
  'war-room-assembler': ['/console', '/console/notifications', ...SHARED_TAIL],
  // Knowledge Synthesizer's console IS the knowledge base (postmortems + KB).
  'knowledge-synthesizer': ['/console/knowledge', '/console/approvals', '/console/health'],
  // Remediation Recommender consumes the RCA verdict and ranks options; the
  // approvals it hands to Auto-Healer are the surface that matters.
  'remediation-recommender': ['/console/rca', '/console/approvals', '/console/health'],
  // Auto-Healer executes a chosen option through the gate — approvals + health.
  'auto-healer': ['/console/approvals', '/console/health'],
  // Topology Discovery's live surface IS the topology map (existing page).
  'topology-discovery':  ['/console/topology', '/console/health'],
};

const DEFAULT_SURFACES = [
  '/console', '/console/alerts', '/console/reasoning', '/console/rca', '/console/notifications', ...SHARED_TAIL,
];

export default function Sidebar() {
  // The agent this console session was opened for (see consoleScope).
  const agent = getAgentById(getConsoleAgentId());
  const requiresApproval = agent?.hitl === 'Required';

  const routes = [...(agent && AGENT_SURFACES[agent.id] ? AGENT_SURFACES[agent.id] : DEFAULT_SURFACES)];
  // Required-HITL agents get the Approvals surface (right after Overview) even
  // if their surface list didn't already include it.
  if (requiresApproval && !routes.includes('/console/approvals')) {
    routes.splice(1, 0, '/console/approvals');
  }
  const nav = routes.map((r) => ITEMS[r]).filter(Boolean);

  // Live count of pending HITL approvals — drives the Approvals badge. Only
  // polled while the Approvals item is actually shown.
  const showApprovals = routes.includes('/console/approvals');
  const { data: approvals } = useFetch(() => api.approvals(false), {
    intervalMs: showApprovals ? 5000 : 0,
  });
  const pending = showApprovals ? approvals?.count ?? 0 : 0;

  return (
    <aside className="hidden w-64 flex-shrink-0 border-r border-ink-200 bg-white dark:border-ink-700 dark:bg-ink-900 md:flex md:flex-col">
      <div className="flex h-16 items-center gap-2 border-b border-ink-200 px-5 dark:border-ink-700">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/15">
          <ShieldCheck className="h-5 w-5 text-accent" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">Adaptive AIOps</span>
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
            {agent ? agent.name : 'Operations Console'}
          </span>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 p-3">
        {nav.map(({ to, label, icon: Icon, end }) => {
          const base =
            'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors';
          const inactive =
            'text-ink-600 hover:bg-ink-100 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-ink-50';
          const badge =
            to === '/console/approvals' && pending > 0 ? (
              <span className="ml-auto inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-warn px-1.5 text-[11px] font-bold text-white">
                {pending}
              </span>
            ) : null;

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
