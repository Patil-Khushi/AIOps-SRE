import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowLeft,
  Plug,
  Boxes,
  Activity,
  Ticket,
  MessageSquare,
  Sparkles,
  Database,
  ShieldCheck,
  Blocks,
  LayoutGrid,
  Star,
  type LucideIcon,
} from 'lucide-react';

// Integrations — the vendor-neutral ecosystem every agent plugs into through
// the platform "seams". Layout mirrors a dense, logo-per-tool integrations
// directory (hero → category filter → counted sections), styled with the
// platform's phase palette + glass aesthetic.
//
// FLAG DISCIPLINE — this page is a capability claim, so the flags are evidence-based:
//   soon: true  → no working implementation in the repo. Being listed in CLAUDE.md's
//                 reference-stack table does NOT make something shipped.
//   top: true   → a real registered provider/adapter AND core to the stack that runs
//                 today. Surfaced under "Top Picks", captioned "runs on today".
//   no flag     → shipped and usable, but not core.
// Dimming a shipped tool, or promoting an aspirational one, misrepresents the product
// on the one page meant to describe it accurately. Verify against code before editing.

interface Tool {
  name: string;
  /** Short monogram shown in the logo tile. */
  mono: string;
  /** Brand-ish accent for the tile. */
  color: string;
  /** Roadmap item — dimmed + "Soon" badge. */
  soon?: boolean;
  /** Surfaced under the "Top picks" filter. */
  top?: boolean;
}

/** Category ids, spelled out so `Filter` below narrows to real literals. Adding a
 *  category here (and forgetting to add it to CATEGORIES, or typoing its id) is a
 *  compile error rather than a silently dead filter chip. */
type CategoryId =
  | 'observability'
  | 'ticketing'
  | 'chatops'
  | 'llm'
  | 'data'
  | 'governance'
  | 'contracts';

interface Category {
  id: CategoryId | 'top';
  title: string;
  note: string;
  icon: LucideIcon;
  /** Section accent, drawn from the four maturity-phase colours. */
  accent: string;
  tools: Tool[];
}

const CATEGORIES: Category[] = [
  {
    id: 'observability',
    title: 'Observability',
    note: 'Metrics, traces, logs and dashboards the agents read from.',
    icon: Activity,
    accent: '#4f46e5',
    tools: [
      { name: 'Prometheus', mono: 'Pr', color: '#e6522c', top: true },
      { name: 'Grafana', mono: 'Gr', color: '#f46800', top: true },
      { name: 'OpenTelemetry', mono: 'OT', color: '#4f8cff', top: true },
      // Jaeger + Loki are the only traces/logs providers and both start on every
      // demo run, so they are core rather than merely available.
      { name: 'Jaeger', mono: 'Jg', color: '#60d0e4', top: true },
      { name: 'Loki', mono: 'Lk', color: '#f9a825', top: true },
      // Tempo has no provider module — it appears only in the docs reference stack.
      { name: 'Tempo', mono: 'Tp', color: '#ff6b35', soon: true },
      { name: 'Datadog', mono: 'Dd', color: '#7c3aed', soon: true },
      { name: 'Elastic', mono: 'Es', color: '#00bfb3', soon: true },
    ],
  },
  {
    id: 'ticketing',
    title: 'Ticketing (ITSM)',
    note: 'Where an incident becomes an official, tracked record.',
    icon: Ticket,
    accent: '#7c3aed',
    tools: [
      { name: 'ServiceNow', mono: 'SN', color: '#62d84e', top: true },
      // aiops/tools/itsm/ ships ServiceNow only; its README describes Jira as a
      // provider that "would land here" as a sibling module.
      { name: 'Jira', mono: 'Jr', color: '#2684ff', soon: true },
      { name: 'Zendesk', mono: 'Zd', color: '#03a17d', soon: true },
    ],
  },
  {
    id: 'chatops',
    title: 'ChatOps & On-call',
    note: 'Who gets told, on which channel, and who gets paged.',
    icon: MessageSquare,
    accent: '#db2777',
    tools: [
      // Slack is fully wired: a Block Kit webhook adapter, an interactive bot, a
      // user map, and the war-room bridge that RA-005+006 posts through.
      { name: 'Slack', mono: 'Sl', color: '#a259c6', top: true },
      { name: 'PagerDuty', mono: 'PD', color: '#06ac38', top: true },
      { name: 'Microsoft Teams', mono: 'Tm', color: '#6264a7', soon: true },
      { name: 'Opsgenie', mono: 'Og', color: '#2684ff', soon: true },
    ],
  },
  {
    id: 'llm',
    title: 'LLM Providers',
    note: 'The reasoning engine — chosen per agent by data sensitivity.',
    icon: Sparkles,
    accent: '#f59e0b',
    tools: [
      { name: 'Anthropic', mono: 'An', color: '#d97757', top: true },
      { name: 'OpenAI', mono: 'AI', color: '#10a37f', top: true },
      // The openai provider auto-detects an .azure.com endpoint and builds a real
      // AzureOpenAI client — this is the platform default every non-RCA agent runs on.
      { name: 'Azure OpenAI', mono: 'Az', color: '#0078d4', top: true },
      { name: 'Ollama (local)', mono: 'Ol', color: '#c4c4cc' },
      { name: 'Amazon Bedrock', mono: 'Br', color: '#ff9900', soon: true },
    ],
  },
  {
    id: 'data',
    title: 'Data & Memory',
    note: 'Incident history and similarity search. SQLite plus in-process embeddings today; dedicated vector and graph stores are the scale path.',
    icon: Database,
    accent: '#4f46e5',
    tools: [
      // What actually backs memory today: SQLModel over SQLite, with embeddings
      // stored as JSON and compared by a brute-force cosine loop in Python.
      { name: 'SQLite', mono: 'SQ', color: '#0f80cc', top: true },
      { name: 'sentence-transformers', mono: 'ST', color: '#ffd21e', top: true },
      // Aspirational: no dependency, no client, no deployed service for any of these.
      { name: 'pgvector', mono: 'pg', color: '#4f8cff', soon: true },
      { name: 'Qdrant', mono: 'Qd', color: '#e11d48', soon: true },
      { name: 'Neo4j', mono: 'N4', color: '#4581ff', soon: true },
      { name: 'Redis', mono: 'Rd', color: '#dc382d', soon: true },
    ],
  },
  {
    id: 'governance',
    title: 'Governance & Runtime',
    note: 'Policy-as-code, safe flag-gated rollout, and orchestration.',
    icon: ShieldCheck,
    accent: '#7c3aed',
    tools: [
      { name: 'Kubernetes', mono: 'K8', color: '#326ce5', top: true },
      // flagd drives every failure injection through the feature_flags seam.
      { name: 'flagd', mono: 'fd', color: '#ffc008', top: true },
      { name: 'GitHub Actions', mono: 'GH', color: '#8b95a8' },
      // policies/hitl.rego is lint-checked in CI but nothing evaluates it at
      // runtime — the HITL gate is still a hard-coded dict in aiops/policy/gate.py.
      { name: 'Open Policy Agent', mono: 'PA', color: '#a78bfa', soon: true },
    ],
  },
  {
    id: 'contracts',
    title: 'Open Contracts',
    note: 'How third-party tools and agents plug in as first-class citizens.',
    icon: Blocks,
    accent: '#db2777',
    tools: [
      // OpenAPI is real via FastAPI's generated schema. MCP and A2A are design
      // vocabulary from the architecture docs — there is no server, client, or
      // transport for either. The tool registry is in-process Python, not MCP.
      { name: 'OpenAPI', mono: 'API', color: '#6ba539' },
      { name: 'MCP', mono: 'MCP', color: '#4f46e5', soon: true },
      { name: 'A2A', mono: 'A2A', color: '#7c3aed', soon: true },
    ],
  },
];

const TOP_PICKS: Category = {
  id: 'top',
  title: 'Top Picks',
  note: 'The core stack the reference POC runs on today.',
  icon: Star,
  accent: '#f59e0b',
  tools: CATEGORIES.flatMap((c) => c.tools.filter((t) => t.top)),
};

// Counted separately so the hero can claim only what actually ships today. Lumping
// roadmap entries into a single "N+ tools" figure would overstate live capability on
// the very page that is meant to represent it accurately.
const LIVE_TOOLS = CATEGORIES.reduce((n, c) => n + c.tools.filter((t) => !t.soon).length, 0);
const ROADMAP_TOOLS = CATEGORIES.reduce((n, c) => n + c.tools.filter((t) => t.soon).length, 0);

/** `'all' | 'top' | string` would widen to plain `string` and silently lose the
 *  literal narrowing, so the category arm is spelled with the real union. */
type Filter = 'all' | 'top' | CategoryId;

const pad = (n: number) => String(n).padStart(2, '0');

function ToolCard({ tool }: { tool: Tool }) {
  return (
    <div
      className={`group relative flex flex-col items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.04] p-5 text-center transition-all duration-200 hover:-translate-y-0.5 hover:border-white/25 hover:bg-white/[0.07] ${
        tool.soon ? 'opacity-60 hover:opacity-100' : ''
      }`}
    >
      {tool.soon && (
        <span
          className="absolute right-2.5 top-2.5 rounded-full border border-white/15 bg-white/10 px-2 py-0.5 font-mono text-[9px] uppercase text-white/50"
          style={{ letterSpacing: '0.12em' }}
        >
          Soon
        </span>
      )}
      {/* Hover glow tinted to the tool's brand accent. */}
      <div
        className="pointer-events-none absolute inset-0 rounded-2xl opacity-0 blur-2xl transition-opacity duration-300 group-hover:opacity-20"
        style={{ background: tool.color }}
      />
      <div
        className="relative flex h-12 w-12 items-center justify-center rounded-xl font-display text-sm font-black"
        style={{
          background: `${tool.color}1f`,
          color: tool.color,
          border: `1px solid ${tool.color}44`,
        }}
      >
        {tool.mono}
      </div>
      <span className="relative font-body text-[13px] font-semibold leading-tight text-white/85">
        {tool.name}
      </span>
    </div>
  );
}

function Section({ cat }: { cat: Category }) {
  const Icon = cat.icon;
  return (
    <section className="space-y-5">
      <div className="flex items-center gap-3">
        <div
          className="flex h-9 w-9 flex-none items-center justify-center rounded-xl"
          style={{ background: `${cat.accent}22`, border: `1px solid ${cat.accent}44` }}
        >
          <Icon className="h-4 w-4" style={{ color: cat.accent }} />
        </div>
        <h2
          className="font-display text-xl font-extrabold uppercase text-white md:text-2xl"
          style={{ letterSpacing: '-0.02em' }}
        >
          {cat.title}
        </h2>
        <span className="font-mono text-xs text-white/35">{pad(cat.tools.length)}</span>
        <div className="ml-1 hidden h-px flex-1 bg-white/10 sm:block" />
        <p className="hidden max-w-xs text-right font-body text-[12px] leading-snug text-white/45 lg:block">
          {cat.note}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
        {cat.tools.map((t) => (
          <ToolCard key={t.name} tool={t} />
        ))}
      </div>
    </section>
  );
}

export default function Integrations() {
  const [filter, setFilter] = useState<Filter>('all');

  const chips = useMemo<{ id: Filter; label: string; icon: LucideIcon }[]>(
    () => [
      { id: 'all', label: 'All', icon: LayoutGrid },
      { id: 'top', label: 'Top Picks', icon: Star },
      ...CATEGORIES.map((c) => ({ id: c.id, label: c.title, icon: c.icon })),
    ],
    [],
  );

  const sections =
    filter === 'all'
      ? CATEGORIES
      : filter === 'top'
        ? [TOP_PICKS]
        : CATEGORIES.filter((c) => c.id === filter);

  return (
    <div className="space-y-14">
      <Link
        to="/agents"
        className="inline-flex items-center gap-2 font-body text-[11px] font-medium uppercase text-white/60 transition-colors hover:text-white"
        style={{ letterSpacing: '0.15em' }}
      >
        <ArrowLeft className="h-4 w-4" /> All agents
      </Link>

      {/* ── Hero ─────────────────────────────────────────────────────── */}
      <div className="flex flex-col items-center text-center">
        <p
          className="flex items-center gap-2 font-mono text-[11px] uppercase text-white/50"
          style={{ letterSpacing: '0.3em' }}
        >
          <Plug className="h-3.5 w-3.5 text-white/70" /> Integrations
        </p>
        <h1
          className="mt-4 max-w-4xl font-display text-5xl font-black uppercase text-white md:text-6xl"
          style={{ letterSpacing: '-0.04em', lineHeight: 0.95 }}
        >
          Plug into{' '}
          <span className="bg-gradient-to-r from-[#4f46e5] via-[#7c3aed] to-[#db2777] bg-clip-text text-transparent">
            every tool
          </span>{' '}
          you already run
        </h1>
        <p className="mt-6 max-w-2xl font-body text-base leading-relaxed text-white/60">
          Adaptive AIOps is wired into{' '}
          <span className="font-semibold text-white/90">{LIVE_TOOLS} tools today</span>, with{' '}
          {ROADMAP_TOOLS} more on the roadmap. Every integration is wrapped behind a thin internal
          interface, so a capability can carry several interchangeable providers — and swapping the
          active one never touches a line of agent code.
        </p>

        {/* Quick stats row. "Live today" counts only unflagged tools; roadmap entries
            are reported separately rather than folded into one inflated total. */}
        <div className="mt-8 flex flex-wrap items-center justify-center gap-x-8 gap-y-3">
          {[
            { k: `${LIVE_TOOLS}`, v: 'Live today' },
            { k: `${ROADMAP_TOOLS}`, v: 'On the roadmap' },
            { k: `${CATEGORIES.length}`, v: 'Categories' },
            { k: '100%', v: 'Vendor-neutral' },
          ].map((s) => (
            <div key={s.v} className="flex items-baseline gap-2">
              <span
                className="font-display text-2xl font-black text-white"
                style={{ letterSpacing: '-0.03em' }}
              >
                {s.k}
              </span>
              <span
                className="font-mono text-[11px] uppercase text-white/45"
                style={{ letterSpacing: '0.12em' }}
              >
                {s.v}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* ── Category filter bar ──────────────────────────────────────── */}
      <div
        role="group"
        aria-label="Filter integrations by category"
        className="-mx-2 flex flex-nowrap gap-2 overflow-x-auto px-2 pb-1 sm:flex-wrap sm:justify-center sm:overflow-visible"
      >
        {chips.map((chip) => {
          const ChipIcon = chip.icon;
          const active = filter === chip.id;
          return (
            <button
              key={chip.id}
              type="button"
              // Toggle semantics: screen readers announce which filter is applied,
              // which the colour change alone does not convey.
              aria-pressed={active}
              onClick={() => setFilter(chip.id)}
              className={`inline-flex flex-none items-center gap-2 rounded-full border px-4 py-2 font-body text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70 focus-visible:ring-offset-2 focus-visible:ring-offset-transparent ${
                active
                  ? 'border-white/80 bg-white text-ink-900'
                  : 'border-white/15 bg-white/5 text-white/70 hover:border-white/30 hover:text-white'
              }`}
            >
              <ChipIcon className="h-3.5 w-3.5" aria-hidden="true" />
              {chip.label}
            </button>
          );
        })}
      </div>

      {/* ── Sections ─────────────────────────────────────────────────── */}
      <div className="space-y-14">
        {sections.map((cat) => (
          <Section key={cat.id} cat={cat} />
        ))}
      </div>

      {/* ── Closing story ────────────────────────────────────────────── */}
      <div className="glass-card flex items-start gap-4 rounded-3xl p-6">
        <div className="flex h-11 w-11 flex-none items-center justify-center rounded-2xl border border-white/15 bg-white/5">
          <Boxes className="h-5 w-5 text-white/80" />
        </div>
        <div>
          <h3
            className="font-display text-base font-extrabold uppercase text-white"
            style={{ letterSpacing: '-0.01em' }}
          >
            One registry, many providers
          </h3>
          <p className="mt-1 max-w-2xl font-body text-[13px] leading-relaxed text-white/55">
            Agents call capabilities like{' '}
            <span className="font-mono text-white/80">itsm.incident.create</span> or{' '}
            <span className="font-mono text-white/80">observability.metrics.query</span> — never a
            vendor SDK directly. The tool registry picks the active provider, which is why Anthropic
            and Azure OpenAI already coexist behind one LLM seam, and why adding a second ITSM or
            logs backend is a configuration change rather than an agent rewrite.
          </p>
        </div>
      </div>
    </div>
  );
}
