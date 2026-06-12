// Which agent the operations console was opened for.
//
// The console at /console is one shared surface that several agents launch
// into (Alert Triage → /console, RCA Agent → /console/reasoning, …). To scope
// agent-specific chrome we remember the launching agent: AgentDetail's
// "Try it" (and a Required agent's "Open Approvals console" link) write its id
// here before navigating into /console; the sidebar reads it to decide, for
// example, whether to show the Approvals item — which only belongs to
// HITL-Required agents. Defaults to Alert Triage, the console's
// Reactive-Active identity, when opened directly (e.g. a bookmark).
export const CONSOLE_AGENT_KEY = 'aiops-console-agent';
export const DEFAULT_CONSOLE_AGENT_ID = 'alert-triage';

export function setConsoleAgent(agentId: string): void {
  try {
    localStorage.setItem(CONSOLE_AGENT_KEY, agentId);
  } catch {
    // localStorage unavailable (privacy mode) — fall back to the default.
  }
}

export function getConsoleAgentId(): string {
  try {
    return localStorage.getItem(CONSOLE_AGENT_KEY) || DEFAULT_CONSOLE_AGENT_ID;
  } catch {
    return DEFAULT_CONSOLE_AGENT_ID;
  }
}

// ── First-run landing gate ──────────────────────────────────────────────────
// The cinematic boot/landing animation should play ONCE per app open, then not
// replay when the user clicks Home during the same session. We use
// sessionStorage (not localStorage) so every fresh tab/visit shows the intro
// once again, and there's no stale flag that suppresses it forever.
const ENTERED_KEY = 'aiops-entered';

export function markEntered(): void {
  try {
    sessionStorage.setItem(ENTERED_KEY, '1');
  } catch {
    // sessionStorage unavailable — landing simply plays each time (no worse than before).
  }
}

export function hasEntered(): boolean {
  try {
    return sessionStorage.getItem(ENTERED_KEY) === '1';
  } catch {
    return false;
  }
}
