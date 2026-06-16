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
  // Toil Detector, Reliability Forecaster, Chaos Orchestrator).
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
  options: Partial<Pick<AgentCatalogItem, 'inputs' | 'outputs' | 'tools' | 'hitl' | 'status' | 'liveSurface' | 'liveSurfaceLabel' | 'liveSurfaceExternal' | 'sre' | 'plainSummary' | 'benefits' | 'howItWorks' | 'setup'>> = {},
): AgentCatalogItem {
  return {
    id: slugify(name),
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
  agent('Alert Triage', 'Reactive-Active', 1, 'Convert live alerts into a structured incident verdict.', 'Normalizes alerts, deduplicates them, enriches them with context, and writes the first verdict.', {
    status: 'Shipped', hitl: 'Optional',
    inputs: ['Prometheus alert', 'service labels', 'metric context'],
    outputs: ['triage verdict', 'severity', 'ownership', 'summary'],
    liveSurface: '/console', liveSurfaceLabel: 'Open triage console',
    plainSummary:
      'When a monitoring alarm goes off, Alert Triage reads it for you. It removes duplicate and flapping alerts, adds context about which service is affected, and writes a clear first verdict — what is wrong, how serious it is, and who owns it — so the on-call engineer does not start from a wall of raw alerts.',
    benefits: [
      'Cuts the time to understand an alert — the first read is done for you.',
      'Hides duplicate and repeating alerts so people see one incident, not fifty.',
      'Every verdict is consistent, explainable, and saved for audit.',
    ],
    howItWorks: [
      'Receives an alert from the monitoring system.',
      'Removes duplicates and adds service + metric context.',
      'Judges how severe it is and who should own it.',
      'Writes a structured verdict to the triage console.',
    ],
    setup: [
      { tool: 'Prometheus / Alertmanager', detail: 'Point the agent at your alert source so it receives firing alerts.' },
      { tool: 'Service catalog / CMDB', detail: 'Provide service-ownership data so each verdict names the right team.' },
      { tool: 'LLM provider', detail: 'Configure a model (Anthropic, OpenAI, or local Ollama) through the platform LLM gateway.' },
    ],
  }),
  agent('Incident Classifier', 'Reactive-Active', 2, 'Classify the verdict using similar historical incidents.', 'Uses similar incidents first, then LLM fallback, then keyword fallback if needed.', {
    status: 'Shipped', hitl: 'Optional',
    inputs: ['triage verdict', 'alert payload', 'historical incidents'],
    outputs: ['incident_type', 'confidence', 'root cause', 'tags'],
    liveSurface: '/classifier', liveSurfaceLabel: 'Open classifier dashboard', liveSurfaceExternal: true,
    plainSummary:
      'Takes the triaged incident and decides what kind of problem it is — infrastructure, application, network, an outside dependency, or a recent change. It does this by comparing the incident to similar past ones, so the incident gets sent to the right team the first time.',
    benefits: [
      'Sends incidents to the correct team faster, with fewer wrong hand-offs.',
      'Learns from history — similar past incidents drive the answer.',
      'Reports a confidence score so people know when to double-check.',
    ],
    howItWorks: [
      'Reads the incident text and turns it into a searchable form.',
      'Finds the most similar past incidents from the knowledge store.',
      'Picks one of five incident types with a confidence score.',
      'Falls back to an AI model, then keywords, when history is thin.',
    ],
    setup: [
      { tool: 'Vector store (pgvector / Qdrant)', detail: 'Holds past incidents the agent compares against.' },
      { tool: 'LLM provider', detail: 'Used for the text embeddings and the AI classification fallback.' },
      { tool: 'Historical incident data', detail: 'Seed the store with labelled past incidents for the best accuracy.' },
    ],
  }),
  agent('Auto-Ticketing', 'Reactive-Active', 3, 'Create or update the ITSM incident record.', 'Writes the service desk ticket and attaches the useful operating context.', {
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
  agent('Runbook Executor', 'Reactive-Active', 4, 'Execute a safe runbook when policy allows it.', 'Turns approved runbook steps into controlled actions with rollback awareness.', {
    hitl: 'Required',
    howItWorks: [
      'Receives an approved remediation step from the policy gate.',
      'Loads the matching runbook and checks its preconditions.',
      'Runs the steps in a controlled, dry-run-first manner.',
      'Verifies the outcome and keeps a tested rollback ready.',
    ],
  }),
  agent('Notification Router', 'Reactive-Active', 5, 'Route notifications to humans and chatops sinks.', 'Chooses the right channel and formats the message for it.', {
    status: 'Shipped', hitl: 'Optional',
    liveSurface: '/console/notifications', liveSurfaceLabel: 'Open notifications stream',
    plainSummary:
      'Decides who needs to hear about an incident and on which channel, then writes a clear, readable message and sends it — so the right people get a useful notification instead of a raw alert dump.',
    benefits: [
      'Right person, right channel, right wording — automatically.',
      'Reduces alert fatigue by routing instead of broadcasting to everyone.',
      'Every notification is logged so you can see what was sent and when.',
    ],
    howItWorks: [
      'Receives the incident and its context.',
      'Chooses the channel and the people to notify.',
      'Formats a clear message for that channel.',
      'Sends it and records the result for audit.',
    ],
    setup: [
      { tool: 'Chat workspace (Slack / Teams)', detail: 'Connect a workspace and app token so the agent can post messages.' },
      { tool: 'Routing rules', detail: 'Define which teams or channels receive which kinds of incident.' },
    ],
  }),
  agent('War-Room Assembler', 'Reactive-Active', 6, 'Create a shared collaboration space for the incident.', 'Brings together the right people, links, and status for the response.', {
    status: 'Shipped', hitl: 'Optional',
    inputs: ['triage verdict', 'CMDB / on-call', 'live telemetry'],
    outputs: ['bridge channel', 'invited SMEs', 'context pack', 'timeline'],
    liveSurface: '/console/war-room', liveSurfaceLabel: 'Open war-room console',
    howItWorks: [
      'Detects that a major incident has been declared.',
      'Spins up a shared channel and bridges the right responders.',
      'Pins the incident summary, dashboards, and live status.',
      'Keeps the space updated until the incident is resolved.',
    ],
  }),
  agent('Log Correlation', 'Reactive-Active', 7, 'Correlate logs, traces, and alerts.', 'Builds the evidence bundle that helps a human see the same incident from multiple angles.', {
    status: 'Shipped',
    howItWorks: [
      'Collects logs, traces, and metrics around the incident window.',
      'Aligns them on a common timeline and service map.',
      'Highlights the correlated signals pointing at the same fault.',
      'Bundles the evidence for the on-call engineer to review.',
    ],
  }),
  agent('Incident Commander', 'Reactive-Active', 8, 'Orchestrate the response end-to-end.', 'Keeps the incident moving, assigns ownership, and coordinates the response.', {
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
  agent('Anomaly Detector', 'Proactive', 9, 'Spot unusual behavior before an incident starts.', 'Finds signals that are deviating from their normal baseline.', {
    howItWorks: [
      'Continuously reads live metrics for each service.',
      'Compares them against a learned normal baseline.',
      'Flags statistically unusual behavior as it emerges.',
      'Raises an early signal before it becomes an incident.',
    ],
  }),
  agent('Drift Monitor', 'Proactive', 10, 'Watch for slow change in behavior.', 'Detects gradual degradation and config drift before a hard outage.', {
    howItWorks: [
      'Tracks configuration and behavior over time.',
      'Compares the current state against a known-good baseline.',
      'Detects slow degradation and configuration drift.',
      'Warns before the drift turns into a hard outage.',
    ],
  }),
  agent('Dependency Mapper', 'Proactive', 11, 'Track service and component dependencies.', 'Builds the dependency picture that explains blast radius.', {
    howItWorks: [
      'Observes live traffic between services.',
      'Builds and maintains the service dependency graph.',
      'Calculates the blast radius for each component.',
      'Feeds the map to triage, RCA, and change-impact agents.',
    ],
  }),
  agent('Noise Reducer', 'Proactive', 12, 'Reduce duplicate and low-value signals.', 'Cuts down on the noise that hides the real issue.', {
    howItWorks: [
      'Ingests the raw alert and signal stream.',
      'Groups duplicates and suppresses flapping signals.',
      'Scores each signal for value and relevance.',
      'Forwards only the meaningful signals downstream.',
    ],
  }),
  agent('Early Warning', 'Proactive', 13, 'Raise heads-up signals before health drops.', 'Turns weak precursors into actionable warnings.', {
    howItWorks: [
      'Watches weak precursor signals across the platform.',
      'Correlates them into a developing risk picture.',
      'Estimates how likely a problem is to materialize.',
      'Raises an actionable heads-up before health drops.',
    ],
  }),
  agent('Topology Discovery', 'Proactive', 14, 'Discover the live platform topology.', 'Keeps a current map of services and relationships.', {
    liveSurface: '/console/topology', liveSurfaceLabel: 'Open topology map',
    howItWorks: [
      'Reads live telemetry and service-to-service traffic.',
      'Discovers services, instances, and their relationships.',
      'Keeps the topology map current as the platform changes.',
      'Publishes the map for other agents and the console.',
    ],
  }),
  agent('Toil Detector', 'Proactive', 15, 'Find repetitive manual work to automate.', 'Identifies routine tasks that should not stay manual.', {
    sre: true,
    howItWorks: [
      'Mines operational logs, tickets, and on-call actions.',
      'Spots repetitive manual tasks that scale with the system.',
      'Quantifies the time and toil each one costs.',
      'Recommends the best candidates to automate.',
    ],
  }),
  agent('Failure Forecaster', 'Predictive', 16, 'Predict the next likely failure area.', 'Ranks where the next outage is most likely to appear.', {
    howItWorks: [
      'Learns from historical failures and current trends.',
      'Scores each service for near-term failure risk.',
      'Ranks where the next outage is most likely.',
      'Surfaces the top risks for proactive attention.',
    ],
  }),
  agent('Capacity Planner', 'Predictive', 17, 'Forecast headroom and scaling needs.', 'Estimates when the platform will need more capacity.', {
    hitl: 'Required',
    howItWorks: [
      'Analyzes usage trends and growth patterns.',
      'Forecasts when headroom will run out.',
      'Recommends scaling actions ahead of demand.',
      'Holds the recommendation for human approval.',
    ],
  }),
  agent('SLO Breach Predictor', 'Predictive', 18, 'Estimate when an SLO is likely to miss.', 'Calculates the chance of breaching an SLO before it happens.', {
    hitl: 'Required',
    howItWorks: [
      'Tracks SLIs against their SLO targets and error budget.',
      'Projects the error-budget burn rate forward in time.',
      'Estimates the probability and timing of a breach.',
      'Raises an approved alert before the budget is spent.',
    ],
  }),
  agent('Seasonality Learner', 'Predictive', 19, 'Learn time-based behavior patterns.', 'Separates normal recurring demand from true anomalies.', {
    howItWorks: [
      'Studies historical demand across days, weeks, and seasons.',
      'Learns the recurring time-based patterns.',
      'Separates normal cycles from true anomalies.',
      'Feeds the baseline to detection and forecasting agents.',
    ],
  }),
  agent('Root-Cause Predictor', 'Predictive', 20, 'Predict likely root cause ahead of time.', 'Ranks probable causes before the incident is fully known.', {
    howItWorks: [
      'Reads early incident signals as they arrive.',
      'Matches them against past incident patterns.',
      'Ranks the most probable root causes.',
      'Gives responders a head start before full RCA.',
    ],
  }),
  agent('Change Impact Predictor', 'Predictive', 21, 'Estimate blast radius for a change.', 'Predicts what the rollout is likely to affect.', {
    hitl: 'Required',
    howItWorks: [
      'Reads a proposed change or deployment.',
      'Maps it against the service dependency graph.',
      'Predicts the likely blast radius and affected services.',
      'Returns a risk assessment for approval before rollout.',
    ],
  }),
  agent('Reliability Forecaster', 'Predictive', 22, 'Forecast the platform reliability trend.', 'Turns history into a forward-looking reliability view.', {
    sre: true,
    howItWorks: [
      'Aggregates reliability history and current trends.',
      'Models the forward-looking reliability trajectory.',
      'Flags where SLOs are trending out of bounds.',
      'Guides where to invest reliability effort next.',
    ],
  }),
  agent('Remediation Recommender', 'Prescriptive-Adaptive', 23, 'Recommend the best fix and why it should work.', 'Produces ranked, safe actions with rollback awareness.', {
    status: 'Shipped',
    hitl: 'Required',
    howItWorks: [
      'Takes the incident verdict and root-cause context.',
      'Generates candidate fixes, each with a rollback.',
      'Ranks them by likely effectiveness and blast radius.',
      'Presents the best option for human approval.',
    ],
  }),
  agent('Auto-Healer', 'Prescriptive-Adaptive', 24, 'Apply safe automated recovery.', 'Runs contained automation to restore service when allowed.', {
    hitl: 'Required',
    howItWorks: [
      'Receives an approved, low-risk remediation.',
      'Runs the contained recovery action (dry-run first).',
      'Watches the service to confirm it recovers.',
      'Rolls back automatically if the fix does not hold.',
    ],
  }),
  agent('Policy Optimizer', 'Prescriptive-Adaptive', 25, 'Tune policy using operational outcomes.', 'Learns which guardrails are too strict or too loose.', {
    hitl: 'Required',
    howItWorks: [
      'Reviews the outcomes of past gated actions.',
      'Finds guardrails that are too strict or too loose.',
      'Proposes tuned policy changes as code.',
      'Submits them for human review before promotion.',
    ],
  }),
  agent('Feedback Learner', 'Prescriptive-Adaptive', 26, 'Learn from fix outcomes.', 'Feeds post-action results back into the system.', {
    hitl: 'Required',
    howItWorks: [
      'Collects the outcome of every applied fix.',
      'Scores what worked and what did not.',
      'Updates models, prompts, and rankings accordingly.',
      'Promotes improvements only after shadow evaluation.',
    ],
  }),
  agent('Cost-Aware Scaler', 'Prescriptive-Adaptive', 27, 'Balance cost and reliability when scaling.', 'Chooses the cheapest action that still keeps risk acceptable.', {
    hitl: 'Required',
    howItWorks: [
      'Reads load, reliability targets, and cost signals.',
      'Weighs scaling options against risk and spend.',
      'Chooses the cheapest action that keeps risk acceptable.',
      'Applies it within the approved guardrails.',
    ],
  }),
  agent('Knowledge Synthesizer', 'Prescriptive-Adaptive', 28, 'Turn incident learning into reusable knowledge.', 'Summarizes what happened into useful operational guidance.', {
    status: 'Shipped', hitl: 'Required',
    liveSurface: '/console/knowledge', liveSurfaceLabel: 'Open knowledge console',
    howItWorks: [
      'Gathers the incident timeline, actions, and outcome.',
      'Drafts a clear postmortem and the lessons learned.',
      'Turns them into reusable runbooks and knowledge.',
      'Publishes for the team after human review.',
    ],
  }),
  agent('Chaos Orchestrator', 'Prescriptive-Adaptive', 29, 'Run controlled chaos experiments.', 'Validates assumptions and resilience safely.', {
    hitl: 'Required',
    sre: true,
    howItWorks: [
      'Defines a controlled experiment with a clear hypothesis.',
      'Sets blast-radius caps and automatic abort conditions.',
      'Injects the failure and observes how the system responds.',
      'Reports findings and reverts safely after approval.',
    ],
  }),
  agent('RCA Agent', 'Prescriptive-Adaptive', 30, 'Produce the root-cause analysis and fix plan.', 'Returns executable remediation steps with rollback awareness.', {
    hitl: 'Required',
    liveSurface: '/console/rca', liveSurfaceLabel: 'Open RCA console',
    inputs: ['incident verdict', 'metrics & traces', 'recent changes', 'runbooks'],
    outputs: ['root cause', 'ranked fix steps', 'rollback plan', 'confidence'],
    plainSummary:
      'The headline agent. Most tools hand you a ranked list of likely causes and stop. The RCA Agent goes further — it works out the real root cause and produces executable fix steps, each with a tested rollback, gated by human approval. Root-cause analysis ends in a resolved incident, not a longer to-do list.',
    benefits: [
      'Turns analysis into action — concrete fix steps, not just suspicions.',
      'Every fix step ships with a tested rollback, so it is safe to apply.',
      'Each step is human-approved before it runs — nothing destructive is automatic.',
    ],
    howItWorks: [
      'Takes the incident verdict plus its metrics, traces, and recent changes.',
      'Reasons over the evidence to find the most probable root cause.',
      'Proposes ranked fix steps, each with a rollback and a blast-radius estimate.',
      'Waits for human approval on every step before anything executes.',
    ],
    setup: [
      { tool: 'Observability (Prometheus / Jaeger)', detail: 'Supplies the metrics and traces the analysis reasons over.' },
      { tool: 'LLM provider', detail: 'Powers the root-cause reasoning over the collected evidence.' },
      { tool: 'Policy gate (OPA / HITL)', detail: 'Enforces human approval on every fix step before it runs.' },
      { tool: 'Automation / runbooks', detail: 'The safe actions a fix step can execute, each with a rollback.' },
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
 