import { useMemo } from 'react';
import ReactFlow, {
  Background, Controls, Node, Edge, MarkerType, Position,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { useTheme } from '@/hooks/useTheme';

// Simple circular layout. For a richer layout, we'd plug in dagre — kept
// out for now to avoid an extra dependency. Works fine for ≤25 services.
function layoutCircular(ids: string[]): Record<string, { x: number; y: number }> {
  const out: Record<string, { x: number; y: number }> = {};
  const r = Math.max(220, ids.length * 18);
  ids.forEach((id, i) => {
    const angle = (2 * Math.PI * i) / Math.max(ids.length, 1);
    out[id] = { x: r * Math.cos(angle), y: r * Math.sin(angle) };
  });
  return out;
}

export default function Topology() {
  const { data, loading, error } = useFetch(api.topology, { intervalMs: 30_000, cacheKey: 'topology' });
  const { theme } = useTheme();

  const { nodes, edges } = useMemo(() => {
    if (!data) return { nodes: [] as Node[], edges: [] as Edge[] };
    const positions = layoutCircular(data.nodes.map((n) => n.id));
    const nodes: Node[] = data.nodes.map((n) => ({
      id: n.id,
      data: { label: n.label },
      position: positions[n.id],
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: {
        borderRadius: 10,
        padding: '8px 14px',
        fontSize: 12,
        fontWeight: 600,
        background: theme === 'dark' ? '#1c2230' : '#ffffff',
        border: theme === 'dark' ? '1px solid #2b3242' : '1px solid #dde2eb',
        color: theme === 'dark' ? '#e6e9ef' : '#0f1320',
        boxShadow: theme === 'dark' ? '0 1px 3px rgba(0,0,0,0.4)' : '0 1px 3px rgba(0,0,0,0.08)',
      },
    }));
    const edges: Edge[] = data.edges.map((e, i) => ({
      id: `e${i}-${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      animated: true,
      markerEnd: { type: MarkerType.ArrowClosed, color: '#4f8cff' },
      style: { stroke: '#4f8cff', strokeWidth: 1.4 },
      label: e.call_count ? `${e.call_count}` : undefined,
      labelStyle: { fill: theme === 'dark' ? '#9aa0aa' : '#5d6779', fontSize: 10 },
      labelBgStyle: { fill: 'transparent' },
    }));
    return { nodes, edges };
  }, [data, theme]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
          Service topology
        </h1>
        <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
          Derived from Jaeger — services + dependency edges from the OpenTelemetry demo cluster.
        </p>
      </div>

      <div className="card overflow-hidden">
        <div className="card-header">
          <h2 className="card-title">
            {data?.nodes.length ?? 0} services · {data?.edges.length ?? 0} edges · source: {data?.source ?? '—'}
          </h2>
        </div>
        <div className="h-[560px]">
          {loading && <LoadingState label="Querying Jaeger…" />}
          {error && <ErrorState error={error} />}
          {!loading && !error && data && data.nodes.length === 0 && (
            <EmptyState
              label="No services discovered"
              hint="Jaeger hasn't reported any services yet. Wait ~30 s after the cluster is up, then refresh."
            />
          )}
          {!loading && !error && data && data.nodes.length > 0 && (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              fitView
              fitViewOptions={{ padding: 0.2 }}
              proOptions={{ hideAttribution: true }}
              minZoom={0.3}
              maxZoom={2}
            >
              <Background gap={20} color={theme === 'dark' ? '#2b3242' : '#dde2eb'} />
              <Controls className="!bg-white dark:!bg-ink-800" showInteractive={false} />
            </ReactFlow>
          )}
        </div>
      </div>

      {data && data.edges.length === 0 && data.nodes.length > 0 && (
        <p className="text-xs text-ink-500 dark:text-ink-400">
          Note: edges come from Jaeger's <code className="font-mono">/api/dependencies</code> endpoint,
          which requires the dependencies job to have run. If you see services but no edges, generate
          load against the demo and retry.
        </p>
      )}
    </div>
  );
}
