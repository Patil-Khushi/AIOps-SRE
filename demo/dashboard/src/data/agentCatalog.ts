// Display mirror of the authoritative agent catalog
// (docs/Adaptive_AIOps_Agent_Catalog.xlsx, per CLAUDE.md). This is a
// hand-maintained copy that drives the dashboard's agent-browser pages
// (Agents / AgentDetail / SreOps) and the per-agent console scoping in the
// Sidebar. It can drift from the xlsx — when the catalog changes there,
// update this file to match. Not a source of truth; a render layer.
//
// Consolidated to 19 agents (6 + 3 + 5 + 5). Several agents were merged into
// combined, product-named agents; the folded members no longer have cards:
//   Reactive-Active (6): Alert Triage + Incident Classifier → Alert Intelligence;
//     Notification Router + War-Room Assembler → Incident Mobilization.
//   Proactive (3): Anomaly Detector + Drift Monitor + Noise Reducer + Early
//     Warning → Proactive Sensing; Topology Discovery + Dependency Mapper →
//     Service Graph.
//   Predictive (5): Failure Forecaster + SLO Breach Predictor + Reliability
//     Forecaster → Reliability Prediction.
//   Prescriptive-Adaptive (5): RCA absorbs Remediation Recommender + Auto-Healer;
//     Feedback Learner + Policy Optimizer → Closed-Loop Learning.

export type AgentPhase =
  | 'Reactive-Active'
  | 'Proactive'
  | 'Predictive'
  | 'Prescriptive-Adaptive';

// One tool/integration a user must configure before the agent is useful.
export interface AgentSetupItem {
  tool: string;
  detail: string;
}

export interface AgentCatalogItem {
  id: string;
  name: string;
  phase: AgentPhase;
  position: number;
  summary: string;
  role: string;
  question: string;
  inputs: string[];
  outputs: string[];
  tools: string[];
  hitl: 'None' | 'Optional' | 'Required';
  status: 'Shipped' | 'Planned';
  liveSurface?: string;
  liveSurfaceLabel?: string;
  // When true, liveSurface is a separate app served at the server root
  // (e.g. /classifier), not an in-SPA route — open it via a full browser
  // navigation rather than React Router.
  liveSurfaceExternal?: boolean;
  // True for the one SRE-specialised agent in each phase (Incident Commander,
  // Toil Detector, Reliability Prediction, Chaos Orchestrator).
  sre?: boolean;
  // ── Introduction-page content (non-technical-friendly). Optional; the
  //    intro page falls back to summary / tools when these are absent. ──
  plainSummary?: string;   // what it does, in everyday language
  benefits?: string[];     // why it matters / what the team gets out of it
  howItWorks?: string[];   // 3–4 simple steps, in order
  setup?: AgentSetupItem[]; // tools/integrations to connect before using it
}

const PHASE_ORDER: AgentPhase[] = [
  'Reactive-Active',
  'Proactive',
  'Predictive',
  'Prescriptive-Adaptive',
];

const PHASE_META: Record<AgentPhase, { title: string; question: string; description: string }> = {
  'Reactive-Active': {
    title: 'Reactive-Active',
    question: 'What just broke?',
    description: 'React to live signals, create the first verdict, and route the incident onward.',
  },
  Proactive: {
    title: 'Proactive',
    question: 'What is starting to look wrong?',
    description: 'Watch for drift, noise, and weak signals before they turn into incidents.',
  },
  Predictive: {
    title: 'Predictive',
    question: 'What will break, and when?',
    description: 'Forecast future failures and prioritize the most likely risk areas.',
  },
  'Prescriptive-Adaptive': {
    title: 'Prescriptive-Adaptive',
    question: 'What should we do, and can the system do it?',
    description: 'Recommend and apply safe actions while learning from the outcome.',
  },
};

const COMMON_INPUTS: Record<AgentPhase, string[]> = {
  'Reactive-Active': ['alert payload', 'service context', 'live telemetry'],
  Proactive: ['historical telemetry', 'topology', 'drift signals'],
  Predictive: ['historical trends', 'seasonality', 'risk signals'],
  'Prescriptive-Adaptive': ['incident context', 'policy state', 'approval state'],
};

const COMMON_OUTPUTS: Record<AgentPhase, string[]> = {
  'Reactive-Active': ['verdict', 'ticket', 'notification'],
  Proactive: ['warning', 'drift report', 'early signal'],
  Predictive: ['forecast', 'risk score', 'pre-incident alert'],
  'Prescriptive-Adaptive': ['recommended fix', 'automation step', 'rollback plan'],
};

const COMMON_TOOLS: Record<AgentPhase, string[]> = {
  'Reactive-Active': ['Prometheus', 'Jaeger', 'CMDB', 'ITSM', 'ChatOps'],
  Proactive: ['Prometheus', 'Topology discovery', 'Baselines', 'Signals'],
  Predictive: ['Forecasting', 'Historical store', 'Risk scoring'],
  'Prescriptive-Adaptive': ['Runbooks', 'Policy gate', 'Automation', 'Feedback loop'],
};

function slugify(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

function agent(
  name: string,
  phase: AgentPhase,
  position: number,
  role: string,
  summary: string,
  options: Partial<Pick<AgentCatalogItem, 'id' | 'inputs' | 'outputs' | 'tools' | 'hitl' | 'status' | 'liveSurface' | 'liveSurfaceLabel' | 'liveSurfaceExternal' | 'sre' | 'plainSummary' | 'benefits' | 'howItWorks' | 'setup'>> = {},
): AgentCatalogItem {
  return {
    // id defaults to the name slug, but can be pinned so a display-name change
    // (e.g. "Alert Triage" → "Alert Intelligence Agent") keeps a stable route +
    // Sidebar/console-scope key. See components/Sidebar.tsx AGENT_SURFACES.
    id: options.id ?? slugify(name),
    name,
    phase,
    position,
    summary,
    role,
    question: PHASE_META[phase].question,
    inputs: options.inputs ?? COMMON_INPUTS[phase],
    outputs: options.outputs ?? COMMON_OUTPUTS[phase],
    tools: options.tools ?? COMMON_TOOLS[phase],
    hitl: options.hitl ?? 'Optional',
    status: options.status ?? 'Planned',
    liveSurface: options.liveSurface,
    liveSurfaceLabel: options.liveSurfaceLabel,
    liveSurfaceExternal: options.liveSurfaceExternal,
    sre: options.sre,
    plainSummary: options.plainSummary,
    benefits: options.benefits,
    howItWorks: options.howItWorks,
    setup: options.setup,
  };
}

export const AGENT_PHASES = PHASE_ORDER;

export const PHASE_DETAILS = PHASE_META;

export const AGENTS: AgentCatalogItem[] = [
  // ── Reactive-Active (6) ────────────────────────────────────────────────────
  agent('Alert Intelligence Agent', 'Reactive-Active', 1, 'Turn live alerts into a classified incident verdict.', 'Normalizes and deduplicates alerts, enriches them with context, writes the first verdict, AND classifies the incident type — triage and classification in one agent.', {
    id: 'alert-triage',
    status: 'Shipped', hitl: 'Optional',
    inputs: ['Prometheus alert', 'service labels', 'metric context'],
    outputs: ['triage verdict', 'severity', 'ownership', 'incident type', 'summary'],
    liveSurface: '/console', liveSurfaceLabel: 'Open alert intelligence console',
    plainSummary:
      'When a monitoring alarm goes off, Alert Intelligence reads it for you. It removes duplicate and flapping alerts, adds context about which service is affected, writes a clear first verdict — what is wrong, how serious it is, and who owns it — and classifies the incident type so the right playbook is picked. It answers both "why am I getting this noisy alert?" and "what does it actually mean?" in one step. (Absorbs the former Alert Triage and Incident Classifier.)',
    benefits: [
      'Cuts the time to understand an alert — the first read AND the classification are done for you.',
      'Hides duplicate and repeating alerts so people see one incident, not fifty.',
      'Every verdict is consistent, explainable, classified, and saved for audit.',
    ],
    howItWorks: [
      'Receives an alert from the monitoring system.',
      'Removes duplicates and adds service + metric context.',
      'Judges how severe it is and who should own it.',
      'Classifies the incident type and maps it to a playbook.',
    ],
    setup: [
      { tool: 'Prometheus / Alertmanager', detail: 'Point the agent at your alert source so it receives firing alerts.' },
      { tool: 'Service catalog / CMDB', detail: 'Provide service-ownership data so each verdict names the right team.' },
      { tool: 'LLM provider', detail: 'Configure a model (Anthropic, OpenAI, or local Ollama) through the platform LLM gateway.' },
    ],
  }),
  agent('Auto-Ticketing', 'Reactive-Active', 2, 'Create or update the ITSM incident record.', 'Writes the service desk ticket and attaches the useful operating context.', {
    status: 'Shipped', hitl: 'Optional',
    inputs: ['verdict', 'classification', 'service context'],
    outputs: ['ticket id', 'ticket state', 'audit trail'],
    plainSummary:
      'Once an incident has a verdict and a category, Auto-Ticketing opens or updates the matching ticket in your IT service desk and attaches all the useful context — so there is always an official record without anyone typing it by hand.',
    benefits: [
      'No manual ticket creation — saves repetitive work and avoids missed records.',
      'Tickets are consistent and carry the full incident context.',
      'Updates the existing ticket instead of creating duplicates.',
    ],
    howItWorks: [
      'Receives the verdict and the incident category.',
      'Finds the matching service-desk ticket, or creates a new one.',
      'Attaches context: affected service, severity, likely cause.',
      'Returns the ticket number and status to the dashboard.',
    ],
    setup: [
      { tool: 'ServiceNow (or Jira)', detail: 'Connect an ITSM instance and credentials so tickets can be created and updated.' },
      { tool: 'Field mapping', detail: 'Map incident fields (severity, assignment group) onto your ITSM form.' },
    ],
  }),
  agent('Runbook Executor', 'Reactive-Active', 3, 'Execute a safe runbook when policy allows it.', 'Turns approved runbook steps into controlled actions with rollback awareness.', {
    hitl: 'Required',
    howItWorks: [
      'Receives an approved remediation step from the policy gate.',
      'Loads the matching runbook and checks its preconditions.',
      'Runs the steps in a controlled, dry-run-first manner.',
      'Verifies the outcome and keeps a tested rollback ready.',
    ],
  }),
  agent('Incident Mobilization Agent', 'Reactive-Active', 4, 'Notify the right people and mobilize the war room for major incidents.', 'Routes one notification to the right people and channel and, on Sev-1/Sev-2, stands up the war room and folds its join link into that same message.', {
    id: 'notification-assembler',
    status: 'Shipped', hitl: 'Optional',
    inputs: ['triage verdict', 'CMDB / on-call', 'live telemetry'],
    outputs: ['one notification', 'war-room link', 'invited SMEs', 'context pack', 'timeline'],
    liveSurface: '/console/notifications', liveSurfaceLabel: 'Open mobilization console',
    plainSummary:
      'Gets the right people into the right place, fast. It decides who needs to hear about an incident and on which channel, writes a single clear message, and sends it. For a major incident (Sev-1/Sev-2) it also spins up a shared war room — channel, on-call expert, live context, and a join link — and folds that link straight into the same notification, so the right people get one message with everything they need instead of two separate pings. Lower severities get the notification only. (Absorbs the former Notification Router and War-Room Assembler.)',
    benefits: [
      'One notification per incident — no duplicate pings to chase.',
      'Right person, right channel, right wording — automatically.',
      'Major incidents get a war room with a join link in the same message.',
      'Reduces alert fatigue by routing instead of broadcasting to everyone.',
    ],
    howItWorks: [
      'Receives the incident verdict and its context.',
      'Chooses the channel and the people to notify (severity + hours + ownership).',
      'On Sev-1/Sev-2, opens the war room and invites the on-call expert.',
      'Sends one message — the notification, with the war-room link folded in.',
    ],
    setup: [
      { tool: 'Chat workspace (Slack / Teams)', detail: 'Connect a workspace and app token so the agent can post and open war-room channels.' },
      { tool: 'On-call / CMDB', detail: 'Provides the on-call expert and ownership so the right people are paged and invited.' },
      { tool: 'Routing rules', detail: 'Define which teams or channels receive which kinds of incident.' },
    ],
  }),
  agent('Log Correlation', 'Reactive-Active', 5, 'Correlate logs, traces, and alerts.', 'Builds the evidence bundle that helps a human see the same incident from multiple angles.', {
    status: 'Shipped',
    // No liveSurface: its live evidence-pack page lives in the ops console at
    // /console/log-correlation; no dedicated per-agent dashboard link here.
    howItWorks: [
      'Collects logs, traces, and metrics around the incident window.',
      'Aligns them on a common timeline and service map.',
      'Highlights the correlated signals pointing at the same fault.',
      'Bundles the evidence for the on-call engineer to review.',
    ],
  }),
  agent('Incident Commander', 'Reactive-Active', 6, 'Orchestrate the response end-to-end.', 'Keeps the incident moving, assigns ownership, and coordinates the response.', {
    status: 'Shipped',
    sre: true,
    liveSurface: '/console/incident-commander', liveSurfaceLabel: 'Open incident command console',
    howItWorks: [
      'Picks up a newly declared incident and its verdict.',
      'Assigns ownership and pulls in the right responders.',
      'Tracks tasks, status, and communications end-to-end.',
      'Drives the incident to resolution and hands off the postmortem.',
    ],
  }),

  // ── Proactive (3) ──────────────────────────────────────────────────────────
  agent('Proactive Sensing Agent', 'Proactive', 7, 'Sense anomalies, drift, and weak signals before they become incidents.', 'Watches live telemetry for anomalies and slow drift, filters out noise, and raises early warnings.', {
    plainSummary:
      "The platform's early-warning sense. It continuously watches live metrics for anomalies and slow drift, groups and suppresses duplicate/flapping noise so the real signal stands out, and turns weak precursors into actionable heads-up warnings — before any of it becomes an incident. (Combines the former Anomaly Detector, Drift Monitor, Noise Reducer, and Early Warning.)",
    howItWorks: [
      'Reads live metrics and signals for every service against a learned baseline.',
      'Flags anomalies and slow drift as they emerge.',
      'Groups duplicates and suppresses flapping so only meaningful signals pass.',
      'Raises an early, actionable warning before health drops.',
    ],
  }),
  agent('Service Graph Agent', 'Proactive', 8, 'Map services and their dependencies, and keep it current.', 'Discovers the live service topology and dependency graph that explains blast radius.', {
    id: 'topology-discovery',
    liveSurface: '/console/topology', liveSurfaceLabel: 'Open service graph',
    plainSummary:
      'Keeps a live map of the platform — every service, instance, and the dependencies between them — so triage, RCA, and change-impact always know what talks to what and what the blast radius of a fault is. (Combines the former Topology Discovery and Dependency Mapper.)',
    howItWorks: [
      'Reads live telemetry and service-to-service traffic.',
      'Discovers services, instances, and their relationships.',
      'Builds the dependency graph and computes blast radius per component.',
      'Keeps the map current and publishes it for other agents and the console.',
    ],
  }),
  agent('Toil Detector', 'Proactive', 9, 'Find repetitive manual work to automate.', 'Identifies routine tasks that should not stay manual.', {
    sre: true,
    howItWorks: [
      'Mines operational logs, tickets, and on-call actions.',
      'Spots repetitive manual tasks that scale with the system.',
      'Quantifies the time and toil each one costs.',
      'Recommends the best candidates to automate.',
    ],
  }),

  // ── Predictive (5) ─────────────────────────────────────────────────────────
  agent('Reliability Prediction Agent', 'Predictive', 10, 'Forecast failures, SLO breaches, and the reliability trend.', 'Predicts where and when reliability will slip — failure risk, SLO-breach timing, and the overall trajectory.', {
    hitl: 'Required',
    sre: true,
    plainSummary:
      'Looks ahead so you fix things before they page. It ranks where the next failure is most likely, projects error-budget burn to estimate when an SLO will breach, and models the forward-looking reliability trend so you know where to invest. (Combines the former Failure Forecaster, SLO Breach Predictor, and Reliability Forecaster.)',
    howItWorks: [
      'Learns from historical failures, SLIs, and current trends.',
      'Scores each service for near-term failure risk.',
      'Projects error-budget burn to estimate SLO-breach timing.',
      'Models the reliability trajectory and raises approved early alerts.',
    ],
  }),
  agent('Capacity Planner', 'Predictive', 11, 'Forecast headroom and scaling needs.', 'Estimates when the platform will need more capacity.', {
    hitl: 'Required',
    howItWorks: [
      'Analyzes usage trends and growth patterns.',
      'Forecasts when headroom will run out.',
      'Recommends scaling actions ahead of demand.',
      'Holds the recommendation for human approval.',
    ],
  }),
  agent('Seasonality Learner', 'Predictive', 12, 'Learn time-based behavior patterns.', 'Separates normal recurring demand from true anomalies.', {
    howItWorks: [
      'Studies historical demand across days, weeks, and seasons.',
      'Learns the recurring time-based patterns.',
      'Separates normal cycles from true anomalies.',
      'Feeds the baseline to detection and forecasting agents.',
    ],
  }),
  agent('Root-Cause Predictor', 'Predictive', 13, 'Predict likely root cause ahead of time.', 'Ranks probable causes before the incident is fully known.', {
    howItWorks: [
      'Reads early incident signals as they arrive.',
      'Matches them against past incident patterns.',
      'Ranks the most probable root causes.',
      'Gives responders a head start before full RCA.',
    ],
  }),
  agent('Change Impact Predictor', 'Predictive', 14, 'Estimate blast radius for a change.', 'Predicts what the rollout is likely to affect.', {
    hitl: 'Required',
    howItWorks: [
      'Reads a proposed change or deployment.',
      'Maps it against the service dependency graph.',
      'Predicts the likely blast radius and affected services.',
      'Returns a risk assessment for approval before rollout.',
    ],
  }),

  // ── Prescriptive-Adaptive (5) ──────────────────────────────────────────────
  agent('RCA Agent', 'Prescriptive-Adaptive', 15, 'Diagnose, recommend, and apply the fix — end to end.', 'Finds the root cause, ranks remediation options, and applies the approved fix with rollback.', {
    hitl: 'Required',
    liveSurface: '/console/rca', liveSurfaceLabel: 'Open RCA console',
    inputs: ['incident verdict', 'metrics & traces', 'recent changes', 'runbooks'],
    outputs: ['root cause', 'ranked remediation options', 'rollback plan', 'execution result', 'confidence'],
    plainSummary:
      'The headline agent, and now the whole prescriptive loop in one. Most tools hand you a ranked list of likely causes and stop. The RCA Agent works out the real root cause, presents a set of ranked remediation options (each with a tested rollback), and — once a human approves one on the same page — applies it (auto-heal), turning the fix off at the source. Root-cause analysis ends in a resolved incident, not a longer to-do list. This absorbs the former Remediation Recommender and Auto-Healer.',
    benefits: [
      'Turns analysis into action — ranked, applicable options, not just suspicions.',
      'Every option ships with a tested rollback, so it is safe to apply.',
      'Approve or deny each option in place — nothing destructive runs without a human.',
      'Applies the approved fix itself and clears the failure — no separate agent to hand off to.',
    ],
    howItWorks: [
      'Takes the incident verdict plus its metrics, traces, and recent changes.',
      'Reasons over the evidence to find the most probable root cause.',
      'Presents ranked remediation options, each with a rollback and a blast-radius estimate.',
      'Waits for human approve/deny on the chosen option — then applies it and confirms recovery.',
    ],
    setup: [
      { tool: 'Observability (Prometheus / Jaeger)', detail: 'Supplies the metrics and traces the analysis reasons over.' },
      { tool: 'LLM provider', detail: 'Powers the root-cause reasoning over the collected evidence.' },
      { tool: 'Policy gate (OPA / HITL)', detail: 'Enforces human approval on every option before it runs.' },
      { tool: 'Automation / runbooks / flags', detail: 'The safe actions an approved option applies, each with a rollback.' },
    ],
  }),
  agent('Knowledge Synthesizer', 'Prescriptive-Adaptive', 16, 'Turn incident learning into reusable knowledge.', 'Summarizes what happened into useful operational guidance.', {
    status: 'Shipped', hitl: 'Required',
    liveSurface: '/console/knowledge', liveSurfaceLabel: 'Open knowledge console',
    howItWorks: [
      'Gathers the incident timeline, actions, and outcome.',
      'Drafts a clear postmortem and the lessons learned.',
      'Turns them into reusable runbooks and knowledge.',
      'Publishes for the team after human review.',
    ],
  }),
  agent('Cost-Aware Scaler', 'Prescriptive-Adaptive', 17, 'Balance cost and reliability when scaling.', 'Chooses the cheapest action that still keeps risk acceptable.', {
    hitl: 'Required',
    howItWorks: [
      'Reads load, reliability targets, and cost signals.',
      'Weighs scaling options against risk and spend.',
      'Chooses the cheapest action that keeps risk acceptable.',
      'Applies it within the approved guardrails.',
    ],
  }),
  agent('Closed-Loop Learning Agent', 'Prescriptive-Adaptive', 18, 'Learn from outcomes and tune the guardrail policy.', 'Feeds fix outcomes back into models and prompts, and tunes the policies that gate actions.', {
    hitl: 'Required',
    plainSummary:
      'Closes the loop so the platform gets better with every incident. It collects the outcome of every applied fix, scores what worked, and updates models, prompts, and rankings — then tunes the guardrail policies that decide what can run automatically, promoting changes only after shadow evaluation and human review. (Combines the former Feedback Learner and Policy Optimizer.)',
    howItWorks: [
      'Collects the outcome of every applied fix and gated action.',
      'Scores what worked and what did not.',
      'Updates models, prompts, and rankings from the results.',
      'Proposes tuned guardrail policy — promoted only after shadow eval + review.',
    ],
  }),
  agent('Chaos Orchestrator', 'Prescriptive-Adaptive', 19, 'Run controlled chaos experiments.', 'Validates assumptions and resilience safely.', {
    hitl: 'Required',
    sre: true,
    howItWorks: [
      'Defines a controlled experiment with a clear hypothesis.',
      'Sets blast-radius caps and automatic abort conditions.',
      'Injects the failure and observes how the system responds.',
      'Reports findings and reverts safely after approval.',
    ],
  }),
];

export function getAgentById(agentId: string): AgentCatalogItem | undefined {
  return AGENTS.find((item) => item.id === agentId);
}

export function agentsByPhase(phase: AgentPhase): AgentCatalogItem[] {
  return AGENTS.filter((item) => item.phase === phase);
}

// The SRE-specialised agent in each phase, in phase order.
export function sreAgents(): AgentCatalogItem[] {
  return AGENTS.filter((item) => item.sre);
}
