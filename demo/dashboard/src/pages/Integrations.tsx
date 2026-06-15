import { Link } from 'react-router-dom';
import { ArrowLeft, Boxes, Plug } from 'lucide-react';

// Integrations — the vendor-neutral ecosystem every agent plugs into through
// the platform "seams". Grounded in CLAUDE.md's reference stack: each category
// has at least two interchangeable options.

interface Category {
  title: string;
  note: string;
  tools: string[];
  swatch: string;
}

const CATEGORIES: Category[] = [
  { title: 'Observability', note: 'Metrics, traces, logs and dashboards the agents read from.', tools: ['Prometheus', 'Jaeger', 'Grafana', 'OpenTelemetry', 'Loki / Tempo'], swatch: '#4f46e5' },
  { title: 'Ticketing (ITSM)', note: 'Where an incident becomes an official, tracked record.', tools: ['ServiceNow', 'Jira'], swatch: '#7c3aed' },
  { title: 'ChatOps & On-call', note: 'Who gets told, on which channel, and who gets paged.', tools: ['Slack', 'Microsoft Teams', 'PagerDuty'], swatch: '#db2777' },
  { title: 'LLM providers', note: 'The reasoning engine — chosen per agent by data sensitivity.', tools: ['Anthropic', 'OpenAI', 'Ollama (local)'], swatch: '#f59e0b' },
  { title: 'Data & memory', note: 'Similarity search over past incidents and service topology.', tools: ['pgvector', 'Qdrant', 'Neo4j'], swatch: '#4f46e5' },
  { title: 'Governance', note: 'Policy-as-code and safe, flag-gated rollout.', tools: ['OPA', 'flagd', 'Kubernetes'], swatch: '#7c3aed' },
  { title: 'Open contracts', note: 'How third-party tools and agents plug in as first-class citizens.', tools: ['MCP', 'A2A', 'OpenAPI'], swatch: '#db2777' },
];

export default function Integrations() {
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
          <Plug className="h-3.5 w-3.5 text-white/70" /> Integrations
        </p>
        <h1
          className="font-display text-5xl font-black uppercase text-white md:text-6xl"
          style={{ letterSpacing: '-0.04em', lineHeight: 0.95 }}
        >
          Vendor-neutral by design
        </h1>
        <p className="max-w-2xl font-body text-base leading-relaxed text-white/60">
          Every integration is wrapped behind a thin internal interface, so each one has at least two
          interchangeable options. Swap any LLM, ticketing, observability, or chat tool without
          touching a single line of agent code.
        </p>
      </div>

      <div className="grid gap-5 md:grid-cols-2 lg:grid-cols-3">
        {CATEGORIES.map((cat) => (
          <div
            key={cat.title}
            className="glass-card relative overflow-hidden rounded-3xl p-6"
            style={{ borderTop: `3px solid ${cat.swatch}` }}
          >
            <div
              className="pointer-events-none absolute -right-10 -top-10 h-32 w-32 rounded-full opacity-25 blur-3xl"
              style={{ background: cat.swatch }}
            />
            <div className="relative">
              <h2 className="font-display text-lg font-extrabold uppercase text-white" style={{ letterSpacing: '-0.01em' }}>
                {cat.title}
              </h2>
              <p className="mt-2 font-body text-[13px] leading-relaxed text-white/55">{cat.note}</p>
              <div className="mt-4 flex flex-wrap gap-2">
                {cat.tools.map((t) => (
                  <span
                    key={t}
                    className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-3 py-1.5 font-mono text-[12px] text-white/80"
                  >
                    <span className="h-1.5 w-1.5 rounded-full" style={{ background: cat.swatch }} />
                    {t}
                  </span>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="glass-card flex items-start gap-4 rounded-3xl p-6">
        <div className="flex h-11 w-11 flex-none items-center justify-center rounded-2xl border border-white/15 bg-white/5">
          <Boxes className="h-5 w-5 text-white/80" />
        </div>
        <div>
          <h3 className="font-display text-base font-extrabold uppercase text-white" style={{ letterSpacing: '-0.01em' }}>
            One registry, many providers
          </h3>
          <p className="mt-1 max-w-2xl font-body text-[13px] leading-relaxed text-white/55">
            Agents call capabilities like <span className="font-mono text-white/80">itsm.incident.create</span> or{' '}
            <span className="font-mono text-white/80">observability.metrics.query</span> — never a vendor SDK
            directly. The tool registry picks the active provider, so switching from ServiceNow to Jira or
            Anthropic to OpenAI is a configuration change, not a rewrite.
          </p>
        </div>
      </div>
    </div>
  );
}
