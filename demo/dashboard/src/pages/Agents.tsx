import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, ChevronRight } from 'lucide-react';
import {
  AGENT_PHASES,
  agentsByPhase,
  PHASE_DETAILS,
  type AgentPhase,
  type AgentCatalogItem,
} from '@/data/agentCatalog';

// Per-phase swatch — the portal maturity palette (see PhasesCard / tailwind config).
const PHASE_SWATCH: Record<AgentPhase, string> = {
  'Reactive-Active':       '#4f46e5', // indigo
  Proactive:               '#7c3aed', // violet
  Predictive:              '#f59e0b', // amber
  'Prescriptive-Adaptive': '#db2777', // pink
};

const CHIP =
  'inline-flex items-center gap-1 rounded-full border border-white/20 bg-white/5 px-2.5 py-1 font-mono text-[11px] text-white/80';

// Two-level drill-down:
//   1. Phase grid           — the four maturity phases.
//   2. Phase → agents grid   — every agent in the chosen phase.
//   3. Agent click           — opens the agent's live dashboard directly when it
//                              has one (liveSurface), else its catalog detail page.
export default function Agents() {
  const navigate = useNavigate();
  const [selectedPhase, setSelectedPhase] = useState<AgentPhase | null>(null);

  const openAgent = (agent: AgentCatalogItem) => {
    navigate(agent.liveSurface ?? `/agents/${agent.id}`);
  };

  if (selectedPhase) {
    return (
      <PhaseAgents
        phase={selectedPhase}
        onBack={() => setSelectedPhase(null)}
        onOpen={openAgent}
      />
    );
  }

  return <PhaseGrid onSelect={setSelectedPhase} />;
}

// ─── level 1 · phase grid ───────────────────────────────────────────────────

function PhaseGrid({ onSelect }: { onSelect: (phase: AgentPhase) => void }) {
  return (
    <div className="space-y-10">
      <div className="space-y-3">
        <p
          className="font-mono text-[11px] uppercase text-white/50"
          style={{ letterSpacing: '0.35em' }}
        >
          Platform Map · 30 Agents · 4 Phases
        </p>
        <h1
          className="font-display text-5xl font-black uppercase text-white md:text-6xl"
          style={{ letterSpacing: '-0.04em', lineHeight: 0.95 }}
        >
          Agents
        </h1>
        <p className="max-w-2xl font-body text-base leading-relaxed text-white/60">
          Pick a phase to see its agents. Each phase answers one operational question —
          then open any agent to jump straight to its dashboard.
        </p>
      </div>

      <div className="grid gap-5 md:grid-cols-2">
        {AGENT_PHASES.map((phase, idx) => {
          const meta = PHASE_DETAILS[phase];
          const agents = agentsByPhase(phase);
          const shipped = agents.filter((a) => a.status === 'Shipped').length;
          const swatch = PHASE_SWATCH[phase];

          return (
            <button
              key={phase}
              type="button"
              onClick={() => onSelect(phase)}
              className="glass-card group relative overflow-hidden rounded-3xl p-6 text-left transition-all duration-300 hover:-translate-y-0.5"
              style={{ borderLeft: `3px solid ${swatch}` }}
            >
              {/* Phase-coloured glow on hover. */}
              <div
                className="pointer-events-none absolute -right-12 -top-12 h-44 w-44 rounded-full opacity-0 blur-3xl transition-opacity duration-500 group-hover:opacity-50"
                style={{ background: swatch }}
              />

              <div className="relative space-y-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <span
                      className="h-2.5 w-2.5 flex-none rounded-full"
                      style={{ backgroundColor: swatch, boxShadow: `0 0 12px ${swatch}` }}
                    />
                    <p
                      className="font-mono text-[10px] uppercase text-white/50"
                      style={{ letterSpacing: '0.3em' }}
                    >
                      Phase {String(idx + 1).padStart(2, '0')}
                    </p>
                  </div>
                  <span className={CHIP}>{String(agents.length).padStart(2, '0')} agents</span>
                </div>

                <div className="space-y-2">
                  <h2
                    className="font-display text-2xl font-extrabold uppercase text-white"
                    style={{ letterSpacing: '-0.02em' }}
                  >
                    {meta.title}
                  </h2>
                  <p className="font-body text-sm font-medium text-white/80">{meta.question}</p>
                  <p className="max-w-xl font-body text-sm text-white/50">{meta.description}</p>
                </div>

                <div className="flex items-center justify-between pt-1">
                  <span
                    className="font-mono text-[10px] uppercase text-white/40"
                    style={{ letterSpacing: '0.2em' }}
                  >
                    {shipped} shipped · {agents.length - shipped} planned
                  </span>
                  <span
                    className="inline-flex items-center gap-1 font-body text-[12px] font-bold uppercase"
                    style={{ color: swatch, letterSpacing: '0.15em' }}
                  >
                    Explore
                    <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                  </span>
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ─── level 2 · agents in the chosen phase ───────────────────────────────────

interface PhaseAgentsProps {
  phase: AgentPhase;
  onBack: () => void;
  onOpen: (agent: AgentCatalogItem) => void;
}

function PhaseAgents({ phase, onBack, onOpen }: PhaseAgentsProps) {
  const meta = PHASE_DETAILS[phase];
  const agents = agentsByPhase(phase);
  const swatch = PHASE_SWATCH[phase];

  return (
    <div className="space-y-8">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-2 font-body text-[11px] font-medium uppercase text-white/70 transition-colors hover:text-white"
        style={{ letterSpacing: '0.15em' }}
      >
        <ArrowLeft className="h-4 w-4" /> All phases
      </button>

      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <span
              className="h-2.5 w-2.5 flex-none rounded-full"
              style={{ backgroundColor: swatch, boxShadow: `0 0 12px ${swatch}` }}
            />
            <p
              className="font-mono text-[11px] uppercase text-white/50"
              style={{ letterSpacing: '0.3em' }}
            >
              {meta.title}
            </p>
          </div>
          <h1
            className="font-display text-4xl font-black uppercase text-white md:text-5xl"
            style={{ letterSpacing: '-0.03em', lineHeight: 0.95 }}
          >
            {meta.question}
          </h1>
          <p className="max-w-3xl font-body text-sm text-white/50">
            {meta.description} Click an agent to open its dashboard.
          </p>
        </div>
        <span className={CHIP}>{String(agents.length).padStart(2, '0')} agents</span>
      </div>

      {agents.length === 0 ? (
        <div className="glass-card rounded-3xl border-dashed p-6 font-body text-sm text-white/50">
          No agents in this phase yet.
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {agents.map((agent) => (
            <button
              key={agent.id}
              type="button"
              onClick={() => onOpen(agent)}
              className="glass-card group relative overflow-hidden rounded-2xl p-5 text-left transition-all duration-300 hover:-translate-y-0.5"
              style={{ borderLeft: `3px solid ${swatch}` }}
            >
              <div
                className="pointer-events-none absolute -right-10 -top-10 h-32 w-32 rounded-full opacity-0 blur-3xl transition-opacity duration-500 group-hover:opacity-40"
                style={{ background: swatch }}
              />

              <div className="relative space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p
                      className="font-mono text-[10px] uppercase text-white/40"
                      style={{ letterSpacing: '0.25em' }}
                    >
                      Agent #{String(agent.position).padStart(2, '0')}
                    </p>
                    <h3
                      className="mt-1 font-display text-lg font-extrabold uppercase text-white"
                      style={{ letterSpacing: '-0.02em' }}
                    >
                      {agent.name}
                    </h3>
                  </div>
                  <ArrowRight className="h-4 w-4 flex-none text-white/60 transition-transform group-hover:translate-x-0.5" />
                </div>

                <p className="line-clamp-2 font-body text-sm text-white/50">{agent.summary}</p>

                <div className="flex flex-wrap items-center gap-1.5">
                  <span className={CHIP}>{agent.hitl} HITL</span>
                  <span className={CHIP}>{agent.status}</span>
                  {agent.liveSurface && <span className={CHIP}>connected</span>}
                </div>

                <div
                  className="inline-flex items-center gap-1 font-body text-[11px] font-bold uppercase"
                  style={{ color: swatch, letterSpacing: '0.15em' }}
                >
                  {agent.liveSurface ? 'Open dashboard' : 'View details'}
                  <ChevronRight className="h-3.5 w-3.5" />
                </div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
