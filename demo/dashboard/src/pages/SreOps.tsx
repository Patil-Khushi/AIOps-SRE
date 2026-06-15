import { Link } from 'react-router-dom';
import { ArrowLeft, ChevronRight, ShieldCheck } from 'lucide-react';
import { sreAgents, type AgentPhase } from '@/data/agentCatalog';

const PHASE_SWATCH: Record<AgentPhase, string> = {
  'Reactive-Active':       '#4f46e5',
  Proactive:               '#7c3aed',
  Predictive:              '#f59e0b',
  'Prescriptive-Adaptive': '#db2777',
};

const CHIP =
  'inline-flex items-center gap-1 rounded-full border border-white/20 bg-white/5 px-2.5 py-1 font-mono text-[11px] text-white/80';

// SRE Ops — the one Site-Reliability-Engineering-specialised agent in each of
// the four phases. Clicking one opens its catalog intro page.
export default function SreOps() {
  const agents = sreAgents();

  return (
    <div className="space-y-10">
      <Link
        to="/agents"
        className="inline-flex items-center gap-2 font-body text-[11px] font-medium uppercase text-white/60 transition-colors hover:text-white"
        style={{ letterSpacing: '0.15em' }}
      >
        <ArrowLeft className="h-4 w-4" /> All agents
      </Link>

      <div className="space-y-3">
        <p className="flex items-center gap-2 font-mono text-[11px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
          <ShieldCheck className="h-3.5 w-3.5 text-white/70" /> SRE Ops
        </p>
        <h1
          className="font-display text-5xl font-black uppercase text-white md:text-6xl"
          style={{ letterSpacing: '-0.04em', lineHeight: 0.95 }}
        >
          Site Reliability
        </h1>
        <p className="max-w-2xl font-body text-base leading-relaxed text-white/60">
          Every phase carries one SRE-specialised agent — the reliability discipline woven through
          the whole pipeline, from running the incident to taming toil, forecasting reliability, and
          rehearsing failure on purpose.
        </p>
      </div>

      <div className="grid gap-5 md:grid-cols-2">
        {agents.map((agent) => {
          const swatch = PHASE_SWATCH[agent.phase];
          return (
            <Link
              key={agent.id}
              to={`/agents/${agent.id}`}
              className="glass-card group relative overflow-hidden rounded-3xl p-6 transition-all duration-300 hover:-translate-y-1"
              style={{ borderTop: `3px solid ${swatch}` }}
            >
              <div
                className="pointer-events-none absolute -right-12 -top-12 h-44 w-44 rounded-full opacity-0 blur-3xl transition-opacity duration-500 group-hover:opacity-50"
                style={{ background: swatch }}
              />
              <div className="relative space-y-3">
                <div className="flex items-center gap-3">
                  <span
                    className="h-2.5 w-2.5 flex-none rounded-full"
                    style={{ backgroundColor: swatch, boxShadow: `0 0 12px ${swatch}` }}
                  />
                  <p className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.25em' }}>
                    {agent.phase} · SRE
                  </p>
                </div>
                <h2 className="font-display text-2xl font-extrabold uppercase text-white" style={{ letterSpacing: '-0.02em' }}>
                  {agent.name}
                </h2>
                <p className="font-body text-sm leading-relaxed text-white/55">{agent.summary}</p>
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  <span className={CHIP}>{agent.hitl} HITL</span>
                  <span className={CHIP}>{agent.status}</span>
                </div>
                <div
                  className="inline-flex items-center gap-1 pt-1 font-body text-[11px] font-bold uppercase"
                  style={{ color: swatch, letterSpacing: '0.15em' }}
                >
                  Open <ChevronRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
                </div>
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
