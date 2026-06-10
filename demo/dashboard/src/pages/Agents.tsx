import { Fragment, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowDown, ArrowLeft, ArrowRight, ChevronRight } from 'lucide-react';
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

  // Every agent opens its introduction page first; the intro page is where the
  // user reads what it does, what to configure, and then clicks "Try it" to
  // launch the live dashboard.
  const openAgent = (agent: AgentCatalogItem) => {
    navigate(`/agents/${agent.id}`);
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
          The Maturity Pipeline
        </h1>
        <p className="max-w-2xl font-body text-base leading-relaxed text-white/60">
          The four phases run in order — each one&apos;s output becomes the next one&apos;s input,
          carrying an incident from <span className="text-white/80">&ldquo;what just broke&rdquo;</span> all
          the way to <span className="text-white/80">&ldquo;the system fixed it&rdquo;</span>. Pick a phase
          to see its agents.
        </p>
      </div>

      {/* Ordered pipeline — horizontal on large screens, stacked on mobile. */}
      <div className="flex flex-col items-stretch gap-4 lg:flex-row">
        {AGENT_PHASES.map((phase, idx) => {
          const meta = PHASE_DETAILS[phase];
          const agents = agentsByPhase(phase);
          const shipped = agents.filter((a) => a.status === 'Shipped').length;
          const swatch = PHASE_SWATCH[phase];
          const last = idx === AGENT_PHASES.length - 1;

          return (
            <Fragment key={phase}>
              <button
                type="button"
                onClick={() => onSelect(phase)}
                className="glass-card group relative flex flex-1 flex-col overflow-hidden rounded-3xl p-6 text-left transition-all duration-300 hover:-translate-y-1"
                style={{ borderTop: `3px solid ${swatch}` }}
              >
                {/* Phase-coloured glow on hover. */}
                <div
                  className="pointer-events-none absolute -right-12 -top-12 h-44 w-44 rounded-full opacity-0 blur-3xl transition-opacity duration-500 group-hover:opacity-50"
                  style={{ background: swatch }}
                />
                {/* Big faded order number — reads as a sequence. */}
                <span
                  className="pointer-events-none absolute -bottom-6 -right-2 font-display text-8xl font-black opacity-[0.07]"
                  style={{ color: swatch }}
                >
                  {idx + 1}
                </span>

                <div className="relative flex flex-1 flex-col">
                  <div className="flex items-center gap-3">
                    <span
                      className="flex h-7 w-7 flex-none items-center justify-center rounded-full font-mono text-[12px] font-bold text-white"
                      style={{ backgroundColor: `${swatch}33`, border: `1px solid ${swatch}` }}
                    >
                      {idx + 1}
                    </span>
                    <p
                      className="font-mono text-[10px] uppercase text-white/50"
                      style={{ letterSpacing: '0.25em' }}
                    >
                      Phase {String(idx + 1).padStart(2, '0')}
                    </p>
                  </div>

                  <h2
                    className="mt-4 font-display text-xl font-extrabold uppercase text-white"
                    style={{ letterSpacing: '-0.02em' }}
                  >
                    {meta.title}
                  </h2>
                  <p className="mt-2 font-body text-sm font-medium text-white/80">{meta.question}</p>
                  <p className="mt-1.5 line-clamp-3 font-body text-[13px] leading-relaxed text-white/50">
                    {meta.description}
                  </p>

                  {/* footer pinned to the bottom so cards line up */}
                  <div className="mt-auto flex items-center justify-between pt-5">
                    <span className={CHIP}>{String(agents.length).padStart(2, '0')} agents</span>
                    <span
                      className="inline-flex items-center gap-1 font-body text-[11px] font-bold uppercase"
                      style={{ color: swatch, letterSpacing: '0.12em' }}
                    >
                      Explore
                      <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                    </span>
                  </div>
                  <p
                    className="mt-2 font-mono text-[10px] uppercase text-white/35"
                    style={{ letterSpacing: '0.15em' }}
                  >
                    {shipped} shipped · {agents.length - shipped} planned
                  </p>
                </div>
              </button>

              {/* Flow connector between phases. */}
              {!last && (
                <div className="flex flex-none items-center justify-center text-white/25">
                  <ArrowRight className="hidden h-6 w-6 lg:block" />
                  <ArrowDown className="h-5 w-5 lg:hidden" />
                </div>
              )}
            </Fragment>
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
            {meta.description} The agents run top to bottom — each one hands its output to the
            next. Click any agent to open it.
          </p>
        </div>
        <span className={CHIP}>{String(agents.length).padStart(2, '0')} agents</span>
      </div>

      {agents.length === 0 ? (
        <div className="glass-card rounded-3xl border-dashed p-6 font-body text-sm text-white/50">
          No agents in this phase yet.
        </div>
      ) : (
        <ol>
          {agents.map((agent, idx) => {
            const last = idx === agents.length - 1;
            return (
              <li key={agent.id} className="relative flex items-start gap-5 pb-5 last:pb-0">
                {/* connector line down to the next agent */}
                {!last && (
                  <span
                    aria-hidden
                    className="absolute left-[17px] top-9 bottom-0 w-px"
                    style={{ background: `linear-gradient(to bottom, ${swatch}99, ${swatch}33)` }}
                  />
                )}

                {/* sequence node */}
                <span
                  className="relative z-10 flex h-[34px] w-[34px] flex-none items-center justify-center rounded-full font-mono text-[12px] font-bold text-white"
                  style={{
                    backgroundColor: `${swatch}33`,
                    border: `1px solid ${swatch}`,
                    boxShadow: `0 0 12px ${swatch}66`,
                  }}
                >
                  {idx + 1}
                </span>

                {/* clickable agent card */}
                <button
                  type="button"
                  onClick={() => onOpen(agent)}
                  className="glass-card group relative flex-1 overflow-hidden rounded-2xl p-5 text-left transition-all duration-300 hover:-translate-y-0.5"
                  style={{ borderLeft: `3px solid ${swatch}` }}
                >
                  <div
                    className="pointer-events-none absolute -right-10 -top-10 h-32 w-32 rounded-full opacity-0 blur-3xl transition-opacity duration-500 group-hover:opacity-40"
                    style={{ background: swatch }}
                  />

                  <div className="relative flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
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
                      <p className="mt-1.5 line-clamp-2 font-body text-sm text-white/50">
                        {agent.summary}
                      </p>
                      <div className="mt-3 flex flex-wrap items-center gap-1.5">
                        <span className={CHIP}>{agent.hitl} HITL</span>
                        <span className={CHIP}>{agent.status}</span>
                        {agent.liveSurface && <span className={CHIP}>connected</span>}
                      </div>
                    </div>

                    <div
                      className="inline-flex items-center gap-1 whitespace-nowrap font-body text-[11px] font-bold uppercase"
                      style={{ color: swatch, letterSpacing: '0.15em' }}
                    >
                      {agent.liveSurface ? 'Open · live' : 'Open'}
                      <ChevronRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
                    </div>
                  </div>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
