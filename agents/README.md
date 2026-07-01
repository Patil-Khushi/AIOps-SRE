# `agents/` — one directory per agent

Phase 1 is in flight. Shipped agents under `agents/`:

| Dir | Catalog ID | Phase | What it does |
|---|---|---|---|
| `alert_triage/` | RA-001 | Reactive-Active | Triages incoming alerts → `TriageVerdict` (service, severity, owning team, dedup). |
| `incident_classifier/` | RA-002 | Reactive-Active | Assigns each verdict an `IncidentType` (infra / app / network / external_dep / change). |
| `auto_ticketing/` | RA-003 | Reactive-Active | Turns a `TriageVerdict` into a ServiceNow PDI ticket via the `aiops.tools.itsm` seam. |
| `notification_assembler/` | RA-005+006 | Reactive-Active | **One agent** that routes the single notification (page / team channel / noise bucket by severity + time-of-day) **and**, on Sev-1/Sev-2, stands up the war room (channel + on-call SME + context pack + seed timeline) and folds its join link into that same message. Merges the former Notification Router (RA-005) + War-Room Assembler (RA-006) into one unit — see the [product decision](#note-ra-005--ra-006-are-one-agent) below. |
| `knowledge_synthesizer/` | PRS-007 | Prescriptive-Adaptive | On a resolved incident, drafts a postmortem + runbook suggestion + KB article (`pending_review`); publication is HITL-gated (`knowledge.publish`). |
| `incident_commander/` | RA-008 (SRE) | Reactive-Active | Coordinates Sev-1/Sev-2 response: chains the reactive flow + RA-007 Log Correlation + RCA via the orchestrator seam, scribes a timeline, posts an IC context pack + human-IC handoff, seeds a postmortem. Takes no destructive action. |

> RA-008 runs on the **orchestrator seam** (`aiops/runtime/orchestrator.py`, INFRA-2 / #74): `run_reactive_flow(alert)` is the single entry point for the RA-001 → RA-002 → RA-003 → RA-005+006 chain that the `/api/triage` route and the auto-triage loop also use. Call it instead of re-wiring the chain.

### Note: RA-005 + RA-006 are one agent

The product ships a **single Notification Assembler** (`notification_assembler/`) that owns both the notification-routing (former RA-005) and war-room (former RA-006) responsibilities, and emits **one** message per incident — with the war-room join link folded in for Sev-1/Sev-2, or a plain notification for lower severities. The two are no longer separate, individually-deployable packages; the earlier thin `notification_router/` and `war_room_assembler/` wrappers were removed. The vision catalog (`docs/Adaptive_AIOps_Agent_Catalog.xlsx`) still lists them as distinct rows; treat "Notification Assembler" as the shipped SKU that fulfils both.

When you add an agent, the contract for it lives in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`. Read its row before writing code — that row is the source of truth for: description, key features, primary tool mapping, secondary integrations, inputs, outputs, HITL level, KPI.

## Layout convention

```
agents/
├── alert_triage/
│   ├── README.md           # short contract summary + how to run locally
│   ├── agent.py            # entry point
│   ├── prompts/
│   │   └── system.md
│   └── evals/
│       ├── golden.json     # hand-built test cases
│       └── README.md       # how to score, what failure means
├── auto_ticketing/
│   └── ...
└── rca/                    # post-POC
    └── ...
```

Directory naming: `<slug>/` — short, lower-case, underscores. The catalog ID lives in the agent's README header (e.g. "RA-001"), not in the directory name.

## Adding an agent (checklist)

1. Find the agent's row in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`. Read it.
2. Create the directory above with `agent.py`, `prompts/`, `evals/golden.json`, `README.md`.
3. Wire LLM calls through `aiops.llm.complete` / `acomplete`. **No direct SDK imports.**
4. Wire tool calls through `aiops.tools.get_registry().call(...)`. **No direct vendor SDKs.**
5. HITL is enforced at the registry boundary. Just call `get_registry().call(capability, ...)` — REQUIRED-level actions return `ok=False` automatically when no approver is wired. Agents don't gate-check themselves.
6. Build the eval set in the same week. A prompt change is a model change — re-run.
7. Set the agent's HITL level in `policies/hitl.rego` to match the catalog.
8. If you introduce a failure scenario for testing, add a truth file under `demo/truth_files/`.

## What goes in `agent.py`

Keep it small — the prompt is the brain, the framework around it is what makes it shippable. Most agents are: parse input → optionally retrieve context via tools → call LLM → validate output → emit structured result → log.

Don't add a fancy class hierarchy until at least three agents share a non-trivial method.
