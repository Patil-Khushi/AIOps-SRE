import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink, ShieldCheck } from 'lucide-react';
import { getAgentById, type AgentPhase } from '@/data/agentCatalog';

const PHASE_SWATCH: Record<AgentPhase, string> = {
  'Reactive-Active':       '#4f46e5',
  Proactive:               '#7c3aed',
  Predictive:              '#f59e0b',
  'Prescriptive-Adaptive': '#db2777',
};

const CHIP =
  'inline-flex items-center gap-1.5 rounded-full border border-white/20 bg-white/5 px-3 py-1 font-mono text-[11px] text-white/80';

function List({ title, items, swatch }: { title: string; items: string[]; swatch: string }) {
  return (
    <div className="glass-card rounded-2xl p-5">
      <p
        className="font-mono text-[10px] uppercase text-white/50"
        style={{ letterSpacing: '0.25em' }}
      >
        {title}
      </p>
      <ul className="mt-3 space-y-2 font-body text-sm text-white/80">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <span
              className="mt-1.5 h-1.5 w-1.5 flex-none rounded-full"
              style={{ backgroundColor: swatch, boxShadow: `0 0 8px ${swatch}` }}
            />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function AgentDetail() {
  const { agentId } = useParams();
  const agent = agentId ? getAgentById(agentId) : undefined;

  if (!agent) {
    return (
      <div className="glass-card max-w-lg space-y-3 rounded-3xl p-6">
        <h1 className="font-display text-2xl font-extrabold uppercase text-white">
          Agent not found
        </h1>
        <p className="font-body text-sm text-white/60">
          The selected agent is not in the catalog.
        </p>
        <Link
          to="/agents"
          className="inline-flex w-fit items-center gap-2 rounded-full bg-white px-5 py-2.5 font-body text-[12px] font-bold uppercase text-black transition-colors hover:bg-white/90"
          style={{ letterSpacing: '0.15em' }}
        >
          <ArrowLeft className="h-4 w-4" /> Back to agents
        </Link>
      </div>
    );
  }

  const swatch = PHASE_SWATCH[agent.phase];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link
            to="/agents"
            className="mb-4 inline-flex items-center gap-2 font-body text-[11px] font-medium uppercase text-white/70 transition-colors hover:text-white"
            style={{ letterSpacing: '0.15em' }}
          >
            <ArrowLeft className="h-4 w-4" /> Back to agents
          </Link>
          <div className="flex items-center gap-3">
            <span
              className="h-2.5 w-2.5 flex-none rounded-full"
              style={{ backgroundColor: swatch, boxShadow: `0 0 12px ${swatch}` }}
            />
            <p
              className="font-mono text-[11px] uppercase text-white/50"
              style={{ letterSpacing: '0.3em' }}
            >
              {agent.phase} · Agent #{String(agent.position).padStart(2, '0')}
            </p>
          </div>
          <h1
            className="mt-2 font-display text-4xl font-black uppercase text-white md:text-5xl"
            style={{ letterSpacing: '-0.03em', lineHeight: 0.95 }}
          >
            {agent.name}
          </h1>
          <p className="mt-3 max-w-3xl font-body text-sm leading-relaxed text-white/60">
            {agent.summary}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span className={CHIP}>
            <ShieldCheck className="h-3.5 w-3.5" /> HITL {agent.hitl}
          </span>
          <span className={CHIP}>{agent.status}</span>
          {agent.liveSurface && (
            <Link
              to={agent.liveSurface}
              className="inline-flex items-center gap-2 rounded-full bg-white px-5 py-2.5 font-body text-[12px] font-bold uppercase text-black transition-colors hover:bg-white/90"
              style={{ letterSpacing: '0.15em' }}
            >
              <ExternalLink className="h-4 w-4" /> {agent.liveSurfaceLabel ?? 'Open surface'}
            </Link>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="glass-card space-y-5 rounded-3xl p-6 lg:col-span-1">
          <div>
            <p
              className="font-mono text-[10px] uppercase text-white/50"
              style={{ letterSpacing: '0.25em' }}
            >
              Dashboard question
            </p>
            <p className="mt-2 font-body text-sm text-white/80">{agent.question}</p>
          </div>
          <div>
            <p
              className="font-mono text-[10px] uppercase text-white/50"
              style={{ letterSpacing: '0.25em' }}
            >
              Role
            </p>
            <p className="mt-2 font-body text-sm text-white/80">{agent.role}</p>
          </div>
          <div>
            <p
              className="font-mono text-[10px] uppercase text-white/50"
              style={{ letterSpacing: '0.25em' }}
            >
              Tools
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {agent.tools.map((tool) => (
                <span key={tool} className={CHIP}>
                  {tool}
                </span>
              ))}
            </div>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2 lg:col-span-2">
          <List title="Inputs" items={agent.inputs} swatch={swatch} />
          <List title="Outputs" items={agent.outputs} swatch={swatch} />
        </div>
      </div>

      <div className="glass-card space-y-3 rounded-3xl p-6">
        <p
          className="font-mono text-[10px] uppercase text-white/50"
          style={{ letterSpacing: '0.25em' }}
        >
          Connected dashboard
        </p>
        <p className="font-body text-sm leading-relaxed text-white/70">
          {agent.liveSurface
            ? 'This card is wired to the live dashboard surface for this agent.'
            : 'This agent currently opens a catalog detail page. The live dashboard can be added later when the agent is implemented.'}
        </p>
      </div>
    </div>
  );
}
