import { Link } from 'react-router-dom';
import type { ComponentType } from 'react';
import {
  ArrowRight,
  Boxes,
  ChevronRight,
  FlaskConical,
  Network,
  RefreshCw,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  Star,
} from 'lucide-react';

// Scrollable marketing sections that live BELOW the immersive hero on the
// landing route. Everything here is grounded in CLAUDE.md / the Solution
// Design — governance principles, the shared runtime, the RCA differentiator —
// so it reads as a real product page, not filler. (The phase list lives in the
// /agents browser, so it's intentionally not duplicated here.)

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="font-mono text-[11px] uppercase text-white/45" style={{ letterSpacing: '0.3em' }}>
      {children}
    </p>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return (
    <h2
      className="mt-3 font-display text-4xl font-black uppercase text-white md:text-5xl"
      style={{ letterSpacing: '-0.03em', lineHeight: 0.95 }}
    >
      {children}
    </h2>
  );
}

// ── governance principles (from CLAUDE.md "non-negotiable design principles") ──
const PRINCIPLES: { icon: ComponentType<{ className?: string }>; title: string; body: string }[] = [
  { icon: Boxes,       title: 'Vendor-neutral',        body: 'Every integration has at least two alternatives. Swap any LLM, ITSM, or observability tool without touching agent code.' },
  { icon: ShieldCheck, title: 'HITL on every action',  body: 'Human-approval gates are enforced by the platform, not the agent — a buggy or compromised agent physically cannot bypass them.' },
  { icon: ScrollText,  title: 'Policy-as-code',        body: 'Every action passes a declarative policy layer (OPA) before it runs. Policy lives in Git and is reviewed like code.' },
  { icon: RotateCcw,   title: 'Safe autonomy',         body: 'Dry-run, blast-radius caps, circuit breakers, and tested rollback are first-class — every action is reversible.' },
  { icon: RefreshCw,   title: 'Closed-loop learning',  body: 'Models, prompts, and policies are versioned and shadow-evaluated before promotion. Auto-rollback on regression.' },
  { icon: FlaskConical,title: 'Evals from day one',    body: 'Each agent ships with its own eval set. A prompt change is a model change — re-run evals before it goes live.' },
];

const RUNTIME = ['Planner', 'Router', 'Orchestrator', 'Memory', 'Tool Registry', 'Eval Harness'];
const CONTRACTS = [
  { name: 'MCP', note: 'tool & data access' },
  { name: 'A2A', note: 'agent-to-agent delegation' },
  { name: 'OpenAPI', note: 'REST integrations' },
];

export default function LandingSections({ onExplore }: { onExplore?: () => void }) {
  return (
    <div className="relative">
      {/* Background that EMBEDS the sections into the hero's world instead of
          cutting to flat black:
            1. the hero's exact vibrant gradient, as a faint wash → shared identity
            2. a dark blend at the very top → seamless join with the hero's base
            3. two soft phase halos → organic depth lower down
          Content (below) sits above all of this via the z-10 wrapper. */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="vibrant-bg absolute inset-0 opacity-[0.13]" />
        <div
          className="absolute inset-x-0 top-0 h-[45vh]"
          style={{ background: 'linear-gradient(to bottom, #050505 0%, rgba(5,5,5,0) 100%)' }}
        />
        <div className="absolute -left-40 top-[44%] h-[26rem] w-[26rem] rounded-full opacity-20 blur-[120px]" style={{ background: '#f59e0b' }} />
        <div className="absolute -right-40 bottom-[4%] h-[26rem] w-[26rem] rounded-full opacity-25 blur-[120px]" style={{ background: '#db2777' }} />
      </div>

      {/* Content sits above the halos. */}
      <div className="relative z-10">

      {/* ════ GOVERNANCE PRINCIPLES ════ */}
      <section className="border-t border-white/5">
        <div className="mx-auto max-w-7xl px-6 py-24 md:px-10 md:py-28">
          <Eyebrow>Built for production trust</Eyebrow>
          <Heading>Governed by design</Heading>
          <p className="mt-4 max-w-2xl font-body text-base leading-relaxed text-white/60">
            Autonomy without guardrails is a liability. These principles are hard constraints,
            not aspirations — baked into the platform layer every agent runs on.
          </p>

          <div className="mt-12 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {PRINCIPLES.map(({ icon: Icon, title, body }) => (
              <div
                key={title}
                className="group rounded-3xl border border-white/10 bg-white/[0.03] p-6 transition-colors hover:bg-white/[0.06]"
              >
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-white/15 bg-white/5">
                  <Icon className="h-5 w-5 text-white/80" />
                </div>
                <h3 className="mt-4 font-display text-lg font-extrabold uppercase text-white" style={{ letterSpacing: '-0.01em' }}>
                  {title}
                </h3>
                <p className="mt-2 font-body text-[14px] leading-relaxed text-white/55">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ════ SHARED RUNTIME + OPEN STANDARDS ════ */}
      <section className="border-t border-white/5">
        <div className="mx-auto max-w-7xl px-6 py-24 md:px-10 md:py-28">
          <div className="grid gap-12 lg:grid-cols-2 lg:items-center">
            <div>
              <Eyebrow>One shared runtime</Eyebrow>
              <Heading>Agents plug into a common brain</Heading>
              <p className="mt-4 max-w-xl font-body text-base leading-relaxed text-white/60">
                Every agent runs on the same Agentic AI Runtime — six components that handle
                planning, routing, orchestration, memory, tools, and evaluation. Build one agent
                or license all thirty; the contract is the same.
              </p>
              <div className="mt-6 flex flex-wrap gap-2">
                {RUNTIME.map((r) => (
                  <span
                    key={r}
                    className="inline-flex items-center gap-1.5 rounded-full border border-white/20 bg-white/5 px-3 py-1.5 font-mono text-[12px] text-white/80"
                  >
                    <Network className="h-3.5 w-3.5 text-white/40" /> {r}
                  </span>
                ))}
              </div>
            </div>

            <div className="glass-card rounded-3xl p-7">
              <p className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.25em' }}>
                Open by default
              </p>
              <p className="mt-3 font-body text-sm text-white/60">
                Third-party tools and agents are first-class citizens through three open contracts:
              </p>
              <div className="mt-5 space-y-3">
                {CONTRACTS.map((c) => (
                  <div key={c.name} className="flex items-center justify-between border-b border-white/10 pb-3 last:border-0 last:pb-0">
                    <span className="font-display text-base font-extrabold uppercase text-white">{c.name}</span>
                    <span className="font-body text-[13px] text-white/50">{c.note}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ════ RCA DIFFERENTIATOR ════ */}
      <section className="border-t border-white/5">
        <div className="mx-auto max-w-7xl px-6 py-24 md:px-10 md:py-28">
          <div
            className="relative overflow-hidden rounded-[2rem] border border-white/10 p-10 md:p-14"
            style={{ background: 'linear-gradient(120deg, #db277733, rgba(255,255,255,0.02) 60%)' }}
          >
            <div className="pointer-events-none absolute -right-20 -top-20 h-72 w-72 rounded-full opacity-40 blur-[100px]" style={{ background: '#db2777' }} />
            <div className="relative max-w-2xl">
              <span className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-white/5 px-3 py-1 font-mono text-[10px] uppercase text-white/70" style={{ letterSpacing: '0.2em' }}>
                <Star className="h-3.5 w-3.5" style={{ color: '#db2777' }} /> Headline differentiator
              </span>
              <h2 className="mt-5 font-display text-3xl font-black uppercase text-white md:text-4xl" style={{ letterSpacing: '-0.02em' }}>
                The RCA Agent
              </h2>
              <p className="mt-4 font-body text-base leading-relaxed text-white/70">
                Most tools hand you a ranked list of likely causes and stop. The RCA Agent produces
                <span className="text-white"> executable fix steps with a tested rollback</span> —
                gated by human approval — so root-cause analysis ends in a resolved incident, not a
                longer to-do list.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ════ CTA + FOOTER ════ */}
      <section className="border-t border-white/5">
        <div className="mx-auto max-w-7xl px-6 py-24 text-center md:px-10 md:py-28">
          <Eyebrow>Take a look</Eyebrow>
          <h2 className="mt-3 font-display text-4xl font-black uppercase text-white md:text-6xl" style={{ letterSpacing: '-0.03em', lineHeight: 0.95 }}>
            Explore the platform
          </h2>
          <p className="mx-auto mt-4 max-w-xl font-body text-base text-white/60">
            Walk the agents phase by phase, or open the live Reactive-Active demo dashboards.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
            {onExplore ? (
              <button
                type="button"
                onClick={onExplore}
                className="inline-flex items-center gap-2 rounded-full bg-white px-7 py-3.5 font-body text-[12px] font-bold uppercase text-black transition-all hover:-translate-y-0.5 hover:bg-white/90"
                style={{ letterSpacing: '0.18em' }}
              >
                Explore Agents <ArrowRight className="h-4 w-4" />
              </button>
            ) : (
              <Link
                to="/agents"
                className="inline-flex items-center gap-2 rounded-full bg-white px-7 py-3.5 font-body text-[12px] font-bold uppercase text-black transition-all hover:-translate-y-0.5 hover:bg-white/90"
                style={{ letterSpacing: '0.18em' }}
              >
                Explore Agents <ArrowRight className="h-4 w-4" />
              </Link>
            )}
            <Link
              to="/console/topology"
              className="inline-flex items-center gap-2 rounded-full border border-white/30 px-7 py-3.5 font-body text-[12px] font-bold uppercase text-white transition-colors hover:bg-white/10"
              style={{ letterSpacing: '0.18em' }}
            >
              View Architecture <ChevronRight className="h-4 w-4" />
            </Link>
          </div>
        </div>

        <footer className="border-t border-white/5">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-8 md:px-10">
            <span className="font-mono text-[11px] uppercase text-white/40" style={{ letterSpacing: '0.2em' }}>
              Adaptive AIOps + SRE Ops
            </span>
            <span className="font-mono text-[11px] uppercase text-white/40" style={{ letterSpacing: '0.2em' }}>
              Platform · v0.1 · POC
            </span>
          </div>
        </footer>
      </section>
      </div>
    </div>
  );
}
