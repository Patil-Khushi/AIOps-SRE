import { CheckCircle2, XCircle, Cpu, Boxes, Database, RefreshCw } from 'lucide-react';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { clsx, timeAgo } from '@/lib/format';
import StatCard from '@/components/StatCard';

export default function SystemHealth() {
  const health = useFetch(api.health, { intervalMs: 10_000 });
  const pods = useFetch(api.pods, { intervalMs: 10_000 });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            System health
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            Backend probes, capability bindings, and pod status from the otel-demo namespace.
          </p>
        </div>
        <button onClick={() => { health.refetch(); pods.refetch(); }} className="btn">
          <RefreshCw className="h-4 w-4" /> Refresh now
        </button>
      </div>

      {/* Top-row probes */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Backend API"
          value={health.error ? 'down' : health.data?.status ?? '…'}
          icon={<Database className="h-4 w-4" />}
          intent={health.error ? 'bad' : health.data?.status === 'ok' ? 'ok' : 'warn'}
          hint={health.data?.checked_at ? `checked ${timeAgo(health.data.checked_at)}` : ''}
        />
        <StatCard
          label="Prometheus"
          value={health.data?.prometheus_reachable ? 'ok' : 'unreachable'}
          icon={<Cpu className="h-4 w-4" />}
          intent={health.data?.prometheus_reachable ? 'ok' : 'bad'}
          hint="observability.metrics.*"
        />
        <StatCard
          label="Jaeger"
          value={health.data?.jaeger_reachable ? 'ok' : 'unreachable'}
          icon={<Cpu className="h-4 w-4" />}
          intent={health.data?.jaeger_reachable ? 'ok' : 'bad'}
          hint="observability.traces.*"
        />
        <StatCard
          label="LLM provider"
          value={health.data?.llm_provider ?? '…'}
          icon={<Boxes className="h-4 w-4" />}
          intent={
            health.data?.llm_provider && health.data.llm_provider !== 'stub' ? 'ok' : 'warn'
          }
          hint="From AIOPS_LLM_PROVIDER"
        />
      </div>

      {/* Capabilities */}
      <div className="card">
        <div className="card-header">
          <h2 className="card-title">Registered tool capabilities</h2>
          <span className="chip">{health.data?.registered_capabilities.length ?? 0}</span>
        </div>
        <div className="card-body">
          {health.loading && !health.data ? (
            <LoadingState />
          ) : health.error ? (
            <ErrorState error={health.error} />
          ) : (
            <div className="flex flex-wrap gap-2">
              {health.data?.registered_capabilities.map((c) => {
                const live = c.startsWith('observability.');
                return (
                  <span key={c} className={clsx('chip', live && '!border-ok/40 !text-ok')}>
                    {live ? <CheckCircle2 className="h-3 w-3" /> : null}
                    {c}
                  </span>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Pods */}
      <div className="card">
        <div className="card-header">
          <h2 className="card-title">
            Pods · namespace {pods.data?.namespace ?? 'otel-demo'}
          </h2>
          <div className="flex items-center gap-2">
            {pods.data && (
              <>
                <span className="chip !border-ok/40 !text-ok">
                  <CheckCircle2 className="h-3 w-3" /> {pods.data.ready_count} ready
                </span>
                {pods.data.not_ready_count > 0 && (
                  <span className="chip !border-bad/40 !text-bad">
                    <XCircle className="h-3 w-3" /> {pods.data.not_ready_count} not ready
                  </span>
                )}
              </>
            )}
          </div>
        </div>
        <div className="card-body !p-0">
          {pods.loading && !pods.data ? (
            <LoadingState label="Running kubectl…" />
          ) : pods.error ? (
            <div className="p-5"><ErrorState error={pods.error} /></div>
          ) : !pods.data || pods.data.pods.length === 0 ? (
            <EmptyState label="No pods found" hint="Is the cluster up?" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-ink-200 text-[11px] uppercase tracking-wider text-ink-500 dark:border-ink-700 dark:text-ink-400">
                    <th className="px-5 py-2 font-medium">Name</th>
                    <th className="px-5 py-2 font-medium">Ready</th>
                    <th className="px-5 py-2 font-medium">Status</th>
                    <th className="px-5 py-2 font-medium">Restarts</th>
                    <th className="px-5 py-2 font-medium">Age</th>
                  </tr>
                </thead>
                <tbody>
                  {pods.data.pods.map((p) => {
                    const okStatus = p.status === 'Running' || p.status === 'Completed';
                    const okReady = (() => {
                      const [a, b] = p.ready.split('/').map((n) => parseInt(n, 10));
                      return Number.isFinite(a) && Number.isFinite(b) && a === b && b > 0;
                    })();
                    return (
                      <tr key={p.name} className="border-b border-ink-200/60 last:border-0 hover:bg-ink-50/60 dark:border-ink-700/60 dark:hover:bg-ink-900/40">
                        <td className="px-5 py-2 font-mono text-xs">{p.name}</td>
                        <td className="px-5 py-2 font-mono text-xs">
                          <span className={clsx(okReady ? 'text-ok' : 'text-warn')}>{p.ready}</span>
                        </td>
                        <td className="px-5 py-2">
                          <span className={clsx(
                            'chip',
                            okStatus ? '!border-ok/40 !text-ok' : '!border-bad/40 !text-bad',
                          )}>{p.status}</span>
                        </td>
                        <td className={clsx('px-5 py-2 font-mono text-xs', p.restarts > 0 && 'text-warn')}>
                          {p.restarts}
                        </td>
                        <td className="px-5 py-2 font-mono text-xs text-ink-500 dark:text-ink-400">{p.age}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
