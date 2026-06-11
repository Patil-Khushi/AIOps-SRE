import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Check, ChevronRight, Rocket, Settings, ShieldCheck, X } from 'lucide-react';
import { getAgentById, type AgentCatalogItem, type AgentPhase } from '@/data/agentCatalog';

const PHASE_SWATCH: Record<AgentPhase, string> = {
  'Reactive-Active':       '#4f46e5',
  Proactive:               '#7c3aed',
  Predictive:              '#f59e0b',
  'Prescriptive-Adaptive': '#db2777',
};

const CHIP =
  'inline-flex items-center gap-1.5 rounded-full border border-white/20 bg-white/5 px-3 py-1 font-mono text-[11px] text-white/80';

// The LLM edition toggle — open-weight/self-hosted vs. paid hosted APIs.
const LLM_EDITIONS = {
  oss:  { label: 'Open Source', note: 'Llama 3 · Mistral · Qwen — self-hosted via Ollama. No API cost, full data control.' },
  paid: { label: 'Paid',        note: 'Claude · GPT · Gemini — hosted API. Top accuracy, usage-based pricing.' },
} as const;
type Edition = keyof typeof LLM_EDITIONS;

// Vendor-neutral integration slots — for each, the interchangeable providers a
// user can plug in. (LLM is handled by the edition toggle, so it's not here.)
type IntegrationKey =
  | 'observability' | 'itsm' | 'cmdb' | 'chatops' | 'oncall' | 'vector' | 'policy' | 'automation';

const INTEGRATIONS: Record<IntegrationKey, { label: string; options: string[] }> = {
  observability: { label: 'Observability',          options: ['Prometheus', 'Datadog', 'CloudWatch', 'Grafana', 'Jaeger'] },
  itsm:          { label: 'Ticketing (ITSM)',       options: ['ServiceNow', 'Jira'] },
  cmdb:          { label: 'Service catalog / CMDB',  options: ['ServiceNow CMDB', 'In-process JSON'] },
  chatops:       { label: 'ChatOps',                 options: ['Slack', 'Microsoft Teams'] },
  oncall:        { label: 'On-call / paging',        options: ['PagerDuty', 'Opsgenie'] },
  vector:        { label: 'Vector store',            options: ['pgvector', 'Qdrant'] },
  policy:        { label: 'Policy / governance',     options: ['OPA'] },
  automation:    { label: 'Automation / runbooks',   options: ['Ansible', 'Kubernetes Jobs', 'Shell runbooks'] },
};

// Infer which integration slots are relevant to an agent from its tools/setup.
function relevantIntegrations(agent: AgentCatalogItem): IntegrationKey[] {
  const hay = [
    ...(agent.setup?.map((s) => s.tool) ?? []),
    ...agent.tools,
  ].join(' ').toLowerCase();
  const keys: IntegrationKey[] = [];
  const add = (k: IntegrationKey) => { if (!keys.includes(k)) keys.push(k); };
  if (/prometheus|metric|trace|jaeger|grafana|observ|datadog|cloudwatch|telemetry|signal|baseline/.test(hay)) add('observability');
  if (/servicenow|itsm|ticket|jira|service desk/.test(hay)) add('itsm');
  if (/cmdb|catalog/.test(hay)) add('cmdb');
  if (/slack|teams|chat/.test(hay)) add('chatops');
  if (/pagerduty|on-call|oncall|paging|routing|notif/.test(hay)) add('oncall');
  if (/vector|pgvector|qdrant|embedding|historical|knowledge/.test(hay)) add('vector');
  if (/opa|policy|hitl|governance|gate/.test(hay)) add('policy');
  if (/runbook|automation|auto-heal|remediat|rollback|chaos|scale/.test(hay)) add('automation');
  if (keys.length === 0) { add('observability'); add('chatops'); }
  return keys;
}

export default function AgentDetail() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const agent = agentId ? getAgentById(agentId) : undefined;
  const [edition, setEdition] = useState<Edition>('oss');
  const [configOpen, setConfigOpen] = useState(false);
  const [picks, setPicks] = useState<Record<string, string>>({});

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
  const steps = agent.howItWorks ?? [];
  const slots = relevantIntegrations(agent);

  return (
    <div className="space-y-6">
      {/* ── top action bar: back + Configure + LLM edition toggle ── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link
          to="/agents"
          className="inline-flex items-center gap-2 font-body text-[11px] font-medium uppercase text-white/60 transition-colors hover:text-white"
          style={{ letterSpacing: '0.15em' }}
        >
          <ArrowLeft className="h-4 w-4" /> All agents
        </Link>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setConfigOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/20 bg-white/5 px-3 py-1.5 font-body text-[11px] font-semibold uppercase text-white/80 transition-colors hover:bg-white/10 hover:text-white"
            style={{ letterSpacing: '0.12em' }}
          >
            <Settings className="h-3.5 w-3.5" /> Configure
          </button>

          {/* LLM edition segmented control */}
          <div className="inline-flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase text-white/40" style={{ letterSpacing: '0.15em' }}>LLM</span>
            <div className="inline-flex rounded-full border border-white/15 bg-white/5 p-0.5">
              {(Object.keys(LLM_EDITIONS) as Edition[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setEdition(k)}
                  className={`rounded-full px-3 py-1 font-body text-[11px] font-semibold uppercase transition-colors ${
                    edition === k ? 'bg-white text-black' : 'text-white/55 hover:text-white'
                  }`}
                  style={{ letterSpacing: '0.1em' }}
                >
                  {LLM_EDITIONS[k].label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ── one screen: left hero | right "how it works" ── */}
      <div className="grid gap-6 lg:grid-cols-2 lg:items-stretch">
        {/* LEFT — hero */}
        <div className="relative overflow-hidden rounded-3xl p-2">
          <div
            className="pointer-events-none absolute -left-10 -top-10 h-56 w-56 rounded-full opacity-30 blur-[90px]"
            style={{ background: swatch }}
          />
          <div className="relative space-y-5">
            <div className="flex items-center gap-3">
              <span className="h-2.5 w-2.5 flex-none rounded-full" style={{ backgroundColor: swatch, boxShadow: `0 0 12px ${swatch}` }} />
              <p className="font-mono text-[11px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
                {agent.phase} · Agent #{String(agent.position).padStart(2, '0')}
              </p>
            </div>

            <h1
              className="font-display text-4xl font-black uppercase text-white md:text-5xl"
              style={{ letterSpacing: '-0.03em', lineHeight: 0.95 }}
            >
              {agent.name}
            </h1>

            <p className="max-w-xl font-body text-base leading-relaxed text-white/75">{plain}</p>

            <div className="flex flex-wrap items-center gap-2">
              <span className={CHIP}>
                <ShieldCheck className="h-3.5 w-3.5" /> HITL {agent.hitl}
              </span>
              <span className={CHIP}>{agent.status}</span>
            </div>

            <div className="space-y-2 pt-1">
              {agent.liveSurface ? (
                <button
                  type="button"
                  onClick={launch}
                  className="inline-flex items-center gap-2 rounded-full bg-white px-7 py-3.5 font-body text-[12px] font-bold uppercase text-black shadow-lg shadow-black/20 transition-all hover:-translate-y-0.5 hover:bg-white/90"
                  style={{ letterSpacing: '0.18em' }}
                >
                  <Rocket className="h-4 w-4" /> Try it
                </button>
              ) : (
                <span
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-white/5 px-7 py-3.5 font-body text-[12px] font-bold uppercase text-white/40"
                  style={{ letterSpacing: '0.18em' }}
                >
                  Dashboard coming soon
                </span>
              )}
              <p className="font-mono text-[10px] uppercase text-white/40" style={{ letterSpacing: '0.12em' }}>
                LLM · {LLM_EDITIONS[edition].label} — {LLM_EDITIONS[edition].note}
              </p>
            </div>
          </div>
        </div>

        {/* RIGHT — how it works */}
        <div className="glass-card rounded-3xl p-6" style={{ borderTop: `3px solid ${swatch}` }}>
          <p className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.25em' }}>
            How it works
          </p>

          {steps.length > 0 ? (
            <ol className="relative mt-5">
              {steps.map((s, i) => {
                const last = i === steps.length - 1;
                return (
                  <li key={s} className="relative flex gap-4 pb-5 last:pb-0">
                    {!last && (
                      <span
                        aria-hidden
                        className="absolute left-[17px] top-9 bottom-0 w-px"
                        style={{ background: `linear-gradient(to bottom, ${swatch}99, ${swatch}22)` }}
                      />
                    )}
                    <span
                      className="relative z-10 flex h-[34px] w-[34px] flex-none items-center justify-center rounded-full font-mono text-[12px] font-bold text-white"
                      style={{ backgroundColor: `${swatch}33`, border: `1px solid ${swatch}` }}
                    >
                      {i + 1}
                    </span>
                    <p className="pt-1.5 font-body text-sm leading-relaxed text-white/80">{s}</p>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="mt-4 font-body text-sm text-white/50">
              Detailed steps for this agent are coming soon. In short: {agent.summary}
            </p>
          )}
        </div>
      </div>

      {/* ── Configure popover: vendor-neutral provider choices ── */}
      {configOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
          <button
            type="button"
            aria-label="Close"
            onClick={() => setConfigOpen(false)}
            className="absolute inset-0 bg-black/70 backdrop-blur-sm"
          />
          <div className="glass-card animate-slide-up relative max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-3xl p-6" style={{ borderTop: `3px solid ${swatch}` }}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.3em' }}>
                  Configure
                </p>
                <h2 className="mt-1.5 font-display text-xl font-extrabold uppercase text-white" style={{ letterSpacing: '-0.02em' }}>
                  Choose your providers
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setConfigOpen(false)}
                aria-label="Close"
                className="flex h-8 w-8 flex-none items-center justify-center rounded-full border border-white/15 text-white/60 transition-colors hover:bg-white/10 hover:text-white"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <p className="mt-2 font-body text-[13px] text-white/50">
              Vendor-neutral by design — pick any provider for each slot this agent uses. Swapping one
              is a config change, not a rewrite.
            </p>

            <div className="mt-5 space-y-5">
              {slots.map((key) => {
                const cat = INTEGRATIONS[key];
                const selected = picks[key] ?? cat.options[0];
                return (
                  <div key={key}>
                    <p className="font-mono text-[10px] uppercase text-white/50" style={{ letterSpacing: '0.2em' }}>
                      {cat.label}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {cat.options.map((opt) => {
                        const active = opt === selected;
                        return (
                          <button
                            key={opt}
                            type="button"
                            onClick={() => setPicks((p) => ({ ...p, [key]: opt }))}
                            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 font-body text-[12px] transition-colors ${
                              active ? 'text-white' : 'border-white/15 bg-white/5 text-white/60 hover:text-white'
                            }`}
                            style={active ? { borderColor: swatch, background: `${swatch}22` } : undefined}
                          >
                            {active && <Check className="h-3.5 w-3.5" style={{ color: swatch }} />}
                            {opt}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>

            <button
              type="button"
              onClick={() => setConfigOpen(false)}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-full bg-white px-5 py-2.5 font-body text-[11px] font-bold uppercase text-black transition-colors hover:bg-white/90"
              style={{ letterSpacing: '0.15em' }}
            >
              Done <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
