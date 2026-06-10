import type { ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Lightbulb,
  Plug,
  Rocket,
  ShieldCheck,
  Workflow,
} from 'lucide-react';
import { getAgentById, type AgentPhase, type AgentSetupItem } from '@/data/agentCatalog';

const PHASE_SWATCH: Record<AgentPhase, string> = {
  'Reactive-Active':       '#4f46e5',
  Proactive:               '#7c3aed',
  Predictive:              '#f59e0b',
  'Prescriptive-Adaptive': '#db2777',
};

const CHIP =
  'inline-flex items-center gap-1.5 rounded-full border border-white/20 bg-white/5 px-3 py-1 font-mono text-[11px] text-white/80';

// Small uppercase eyebrow above each section — the recurring "website" rhythm.
function Eyebrow({ icon, swatch, children }: { icon: ReactNode; swatch: string; children: ReactNode }) {
  return (
    <p className="flex items-center gap-2 font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
      <span style={{ color: swatch }}>{icon}</span>
      {children}
    </p>
  );
}

export default function AgentDetail() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const agent = agentId ? getAgentById(agentId) : undefined;

  if (!agent) {
    return (
      <div className="glass-card max-w-lg space-y-3 rounded-3xl p-6">
        <h1 className="font-display text-2xl font-extrabold uppercase text-white">Agent not found</h1>
        <p className="font-body text-sm text-white/60">The selected agent is not in the catalog.</p>
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

  const launch = () => {
    if (!agent.liveSurface) return;
    if (agent.liveSurfaceExternal) window.location.assign(agent.liveSurface);
    else navigate(agent.liveSurface);
  };

  const plain = agent.plainSummary ?? agent.summary;
  const benefits = agent.benefits ?? [];
  const steps = agent.howItWorks ?? [];
  const setup: AgentSetupItem[] =
    agent.setup ?? agent.tools.map((t) => ({ tool: t, detail: `Connect ${t} to this agent.` }));

  const TryButton = ({ block = false }: { block?: boolean }) =>
    agent.liveSurface ? (
      <button
        type="button"
        onClick={launch}
        className={`inline-flex items-center justify-center gap-2 rounded-full bg-white px-7 py-3.5 font-body text-[12px] font-bold uppercase text-black shadow-lg shadow-black/20 transition-all hover:-translate-y-0.5 hover:bg-white/90 ${block ? 'w-full' : ''}`}
        style={{ letterSpacing: '0.18em' }}
      >
        <Rocket className="h-4 w-4" /> Try it
      </button>
    ) : (
      <span
        className={`inline-flex items-center justify-center gap-2 rounded-full border border-white/20 bg-white/5 px-7 py-3.5 font-body text-[12px] font-bold uppercase text-white/40 ${block ? 'w-full' : ''}`}
        style={{ letterSpacing: '0.18em' }}
      >
        Dashboard coming soon
      </span>
    );

  return (
    <div className="space-y-20 pb-8">
      {/* ════ HERO ════ */}
      <section className="relative">
        {/* big ambient swatch glow behind the hero */}
        <div
          className="pointer-events-none absolute -top-24 left-1/4 h-[28rem] w-[28rem] -translate-x-1/2 rounded-full opacity-30 blur-[120px]"
          style={{ background: swatch }}
        />
        <div className="relative">
          <Link
            to="/agents"
            className="inline-flex items-center gap-2 font-body text-[11px] font-medium uppercase text-white/60 transition-colors hover:text-white"
            style={{ letterSpacing: '0.15em' }}
          >
            <ArrowLeft className="h-4 w-4" /> All agents
          </Link>

          <div className="mt-8 grid items-start gap-10 lg:grid-cols-[1.4fr_1fr]">
            {/* left — identity + CTA */}
            <div className="space-y-6">
              <div className="flex items-center gap-3">
                <span className="h-2.5 w-2.5 flex-none rounded-full" style={{ backgroundColor: swatch, boxShadow: `0 0 12px ${swatch}` }} />
                <p className="font-mono text-[11px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
                  {agent.phase} · Agent #{String(agent.position).padStart(2, '0')}
                </p>
              </div>

              <h1
                className="font-display text-5xl font-black uppercase text-white md:text-6xl"
                style={{ letterSpacing: '-0.04em', lineHeight: 0.92 }}
              >
                {agent.name}
              </h1>

              <p className="max-w-2xl font-body text-lg leading-relaxed text-white/75">{plain}</p>

              <div className="flex flex-wrap items-center gap-3 pt-2">
                <TryButton />
                <span className="font-body text-[12px] text-white/40">
                  {agent.liveSurface
                    ? 'Opens the working demo dashboard.'
                    : 'On the roadmap — dashboard not built yet.'}
                </span>
              </div>
            </div>

            {/* right — quick-facts panel */}
            <div className="glass-card rounded-3xl p-6" style={{ borderTop: `3px solid ${swatch}` }}>
              <Eyebrow icon={<ShieldCheck className="h-3.5 w-3.5" />} swatch={swatch}>
                At a glance
              </Eyebrow>
              <dl className="mt-4 divide-y divide-white/10">
                {[
                  ['Phase', agent.phase],
                  ['Status', agent.status],
                  ['Human-in-the-loop', agent.hitl],
                  ['Live dashboard', agent.liveSurface ? 'Available' : 'Coming soon'],
                ].map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between gap-3 py-2.5">
                    <dt className="font-body text-[13px] text-white/50">{k}</dt>
                    <dd className="font-body text-[13px] font-semibold text-white/90">{v}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </div>
        </div>
      </section>

      {/* ════ WHY IT MATTERS ════ */}
      {benefits.length > 0 && (
        <section className="space-y-8">
          <div className="space-y-3">
            <Eyebrow icon={<Lightbulb className="h-3.5 w-3.5" />} swatch={swatch}>
              Why it matters
            </Eyebrow>
            <h2 className="font-display text-3xl font-extrabold uppercase text-white" style={{ letterSpacing: '-0.02em' }}>
              What your team gets
            </h2>
          </div>
          <div className="grid gap-5 md:grid-cols-3">
            {benefits.map((b) => (
              <div key={b} className="group rounded-3xl border border-white/10 bg-white/[0.03] p-6 transition-colors hover:bg-white/[0.06]">
                <div
                  className="flex h-11 w-11 items-center justify-center rounded-2xl"
                  style={{ backgroundColor: `${swatch}22`, border: `1px solid ${swatch}55` }}
                >
                  <CheckCircle2 className="h-5 w-5" style={{ color: swatch }} />
                </div>
                <p className="mt-4 font-body text-[15px] leading-relaxed text-white/80">{b}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ════ HOW IT WORKS ════ */}
      {steps.length > 0 && (
        <section className="space-y-8">
          <div className="space-y-3">
            <Eyebrow icon={<Workflow className="h-3.5 w-3.5" />} swatch={swatch}>
              How it works
            </Eyebrow>
            <h2 className="font-display text-3xl font-extrabold uppercase text-white" style={{ letterSpacing: '-0.02em' }}>
              From signal to answer
            </h2>
          </div>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {steps.map((s, i) => (
              <div key={s} className="relative overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03] p-6">
                {/* big faded step watermark */}
                <span
                  className="pointer-events-none absolute -right-2 -top-4 font-display text-7xl font-black opacity-10"
                  style={{ color: swatch }}
                >
                  {i + 1}
                </span>
                <span
                  className="flex h-8 w-8 items-center justify-center rounded-full font-mono text-[12px] font-bold text-white"
                  style={{ backgroundColor: `${swatch}33`, border: `1px solid ${swatch}` }}
                >
                  {i + 1}
                </span>
                <p className="relative mt-4 font-body text-sm leading-relaxed text-white/80">{s}</p>
                {i < steps.length - 1 && (
                  <ArrowRight className="absolute bottom-5 right-5 h-4 w-4 text-white/20" />
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ════ TOOLS TO CONFIGURE ════ */}
      <section className="space-y-8">
        <div className="space-y-3">
          <Eyebrow icon={<Plug className="h-3.5 w-3.5" />} swatch={swatch}>
            Tools to configure
          </Eyebrow>
          <h2 className="font-display text-3xl font-extrabold uppercase text-white" style={{ letterSpacing: '-0.02em' }}>
            Connect once, run anywhere
          </h2>
          <p className="max-w-2xl font-body text-sm text-white/50">
            Wire these up before going live. For this demo they are pre-connected to synthetic data,
            so you can press <span className="text-white/80">Try it</span> right away.
          </p>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          {setup.map((s, i) => (
            <div key={s.tool} className="flex gap-4 rounded-3xl border border-white/10 bg-white/[0.03] p-5">
              <div
                className="flex h-10 w-10 flex-none items-center justify-center rounded-xl font-mono text-[12px] font-bold"
                style={{ backgroundColor: `${swatch}22`, color: swatch, border: `1px solid ${swatch}44` }}
              >
                {String(i + 1).padStart(2, '0')}
              </div>
              <div>
                <p className="font-body text-[15px] font-semibold text-white">{s.tool}</p>
                <p className="mt-1 font-body text-[13px] leading-relaxed text-white/55">{s.detail}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ════ UNDER THE HOOD (technical) ════ */}
      <section className="space-y-6">
        <Eyebrow icon={<ShieldCheck className="h-3.5 w-3.5" />} swatch={swatch}>
          Under the hood
        </Eyebrow>
        <div className="grid gap-5 md:grid-cols-3">
          <div className="rounded-3xl border border-white/10 bg-white/[0.03] p-5">
            <p className="font-mono text-[10px] uppercase text-white/40" style={{ letterSpacing: '0.2em' }}>Inputs</p>
            <ul className="mt-3 space-y-2 font-body text-sm text-white/75">
              {agent.inputs.map((x) => (
                <li key={x} className="flex gap-2">
                  <span className="mt-1.5 h-1.5 w-1.5 flex-none rounded-full" style={{ backgroundColor: swatch }} />
                  {x}
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-3xl border border-white/10 bg-white/[0.03] p-5">
            <p className="font-mono text-[10px] uppercase text-white/40" style={{ letterSpacing: '0.2em' }}>Outputs</p>
            <ul className="mt-3 space-y-2 font-body text-sm text-white/75">
              {agent.outputs.map((x) => (
                <li key={x} className="flex gap-2">
                  <span className="mt-1.5 h-1.5 w-1.5 flex-none rounded-full" style={{ backgroundColor: swatch }} />
                  {x}
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-3xl border border-white/10 bg-white/[0.03] p-5">
            <p className="font-mono text-[10px] uppercase text-white/40" style={{ letterSpacing: '0.2em' }}>Tools used</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {agent.tools.map((t) => (
                <span key={t} className={CHIP}>{t}</span>
              ))}
            </div>
          </div>
        </div>
        <p className="font-body text-[13px] text-white/55">
          <span className="text-white/70">Role:</span> {agent.role}
        </p>
      </section>

      {/* ════ CTA BAND ════ */}
      <section
        className="relative overflow-hidden rounded-[2rem] border border-white/10 p-10 text-center md:p-14"
        style={{ background: `linear-gradient(120deg, ${swatch}33, rgba(255,255,255,0.02) 60%)` }}
      >
        <div
          className="pointer-events-none absolute -bottom-24 right-0 h-72 w-72 rounded-full opacity-40 blur-[100px]"
          style={{ background: swatch }}
        />
        <div className="relative mx-auto max-w-xl space-y-5">
          <h2 className="font-display text-3xl font-black uppercase text-white md:text-4xl" style={{ letterSpacing: '-0.02em' }}>
            {agent.liveSurface ? 'See it in action' : 'Coming to a future release'}
          </h2>
          <p className="font-body text-sm text-white/60">
            {agent.liveSurface
              ? `Launch the ${agent.name} dashboard and watch it work on live demo data.`
              : `${agent.name} is on the roadmap. Explore the shipped agents in the meantime.`}
          </p>
          <div className="flex justify-center pt-1">
            {agent.liveSurface ? <TryButton /> : (
              <Link
                to="/agents"
                className="inline-flex items-center gap-2 rounded-full bg-white px-7 py-3.5 font-body text-[12px] font-bold uppercase text-black transition-colors hover:bg-white/90"
                style={{ letterSpacing: '0.18em' }}
              >
                <ArrowLeft className="h-4 w-4" /> Back to agents
              </Link>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
