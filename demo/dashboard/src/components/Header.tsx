import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Moon, Sun, Activity, AlertCircle } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { api } from '@/lib/api';
import type { HealthResponse } from '@/types/api';
import { clsx } from '@/lib/format';
import { getConsoleAgentId } from '@/lib/consoleScope';
import { getAgentById } from '@/data/agentCatalog';

function ChipDot({
  ok,
  label,
  title,
}: { ok: boolean; label: string; title?: string }) {
  return (
    <span className="chip" title={title}>
      <span className={clsx('h-1.5 w-1.5 rounded-full', ok ? 'bg-ok' : 'bg-bad')} />
      {label}
    </span>
  );
}

export default function Header() {
  const { theme, toggle } = useTheme();
  const navigate = useNavigate();
  const [h, setH] = useState<HealthResponse | null>(null);
  const [err, setErr] = useState(false);

  // Back goes to the agent this console was opened for (not home). The agent
  // detail page is the step before the console, so this is a true "up one level".
  const agentId = getConsoleAgentId();
  const agent = getAgentById(agentId);
  const goBack = () => navigate(agent ? `/agents/${agent.id}` : '/agents');

  useEffect(() => {
    let alive = true;
    const fetch = () => api.health().then(
      (data) => alive && (setH(data), setErr(false)),
      () => alive && setErr(true),
    );
    fetch();
    const t = setInterval(fetch, 15_000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-ink-200 bg-white/80 px-6 backdrop-blur-md dark:border-ink-700 dark:bg-ink-900/80">
      <div className="flex items-center gap-2">
        <button
          onClick={goBack}
          className="btn !p-1.5"
          title={agent ? `Back to ${agent.name}` : 'Back to agents'}
          aria-label="Back"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <Activity className="h-4 w-4 text-accent" />
        <h1 className="text-sm font-semibold text-ink-900 dark:text-ink-50">
          {agent ? agent.name : 'Operations Console'}
        </h1>
      </div>

      <div className="flex items-center gap-2">
        {err ? (
          <span className="chip border-bad/40 text-bad">
            <AlertCircle className="h-3 w-3" /> backend offline
          </span>
        ) : h ? (
          <>
            <ChipDot ok={h.prometheus_reachable} label="Prometheus" />
            <ChipDot ok={h.jaeger_reachable}     label="Jaeger" />
            <ChipDot
              ok={h.llm_ok}
              label={`LLM · ${h.llm_provider ?? '—'}`}
              title={
                h.llm_ok
                  ? `${h.llm_model ?? ''} · ${h.llm_latency_ms ?? '?'}ms${h.llm_cached ? ' (cached)' : ''}`
                  : h.llm_error ?? 'LLM unreachable'
              }
            />
          </>
        ) : (
          <span className="chip">connecting…</span>
        )}
        <button
          onClick={toggle}
          aria-label="Toggle theme"
          className="btn ml-2 !p-1.5"
          title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
      </div>
    </header>
  );
}
