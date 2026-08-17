// Every Python `@property` on the Investigation tree that does NOT survive
// `model_dump()` (Pydantic doesn't serialize properties), re-implemented once
// here so no component re-derives it inconsistently. Each function names the
// Python source it mirrors (agents/rca_agent/investigation/models.py) so a
// backend rename has a findable counterpart.
//
// Every exported function returns a `Derived<T>` discriminated result rather
// than throwing — a renamed/missing field degrades one tab's section to
// `UnavailableState`, never crashes the page. The investigation package is
// still mid-upgrade (uncommitted) when this was written; that's the risk this
// guards against.

import type {
  BlastRadiusReport,
  EvidenceMatrix,
  EvidenceStance,
  Investigation,
  MemoryStatus,
  RiskAssessment,
  RootCauseStatus,
  ServiceImpact,
} from '@/types/rca';

export type Derived<T> = { ok: true; value: T } | { ok: false; reason: string };

function ok<T>(value: T): Derived<T> {
  return { ok: true, value };
}
function fail<T>(reason: string): Derived<T> {
  return { ok: false, reason };
}

// ─── blast radius grouping ──────────────────────────────────────────────────
// Mirrors BlastRadiusReport.directly_affected/.indirectly_affected/etc.
// (investigation/models.py:781-817, the `_of()` helper). Always returns all
// five buckets, even empty — a tab must not omit one by truthiness-checking.
// `ran: false` means the stage did not run at all (`blast_radius === null`),
// which is different from "ran and found nothing" (ran: true, all empty).

export interface BlastRadiusGroups {
  directly_affected: ServiceImpact[];
  indirectly_affected: ServiceImpact[];
  observed_healthy: ServiceImpact[];
  not_observed: ServiceImpact[];
  unknown: ServiceImpact[];
  topologyAvailable: boolean;
  ran: boolean;
}

export function groupBlastRadius(report: BlastRadiusReport | null | undefined): Derived<BlastRadiusGroups> {
  if (!report) {
    return ok({
      directly_affected: [],
      indirectly_affected: [],
      observed_healthy: [],
      not_observed: [],
      unknown: [],
      topologyAvailable: false,
      ran: false,
    });
  }
  try {
    const by = (state: ServiceImpact['state']) => report.impacts.filter((i) => i.state === state);
    return ok({
      directly_affected: by('directly_affected'),
      indirectly_affected: by('indirectly_affected'),
      observed_healthy: by('observed_healthy'),
      not_observed: by('not_observed'),
      unknown: by('unknown'),
      topologyAvailable: report.topology_available,
      ran: true,
    });
  } catch (e) {
    return fail(`groupBlastRadius failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}

// ─── risk ───────────────────────────────────────────────────────────────────
// Mirrors RiskAssessment.unassessed (investigation/models.py:854). Tri-state
// fields: `null` means "not assessed", and must never collapse to "safe".

const RISK_BOOL_FIELDS = [
  'causes_downtime',
  'interrupts_active_requests',
  'risks_data_loss',
  'risks_duplicate_transactions',
  'affects_downstream',
  'affects_upstream',
  'destroys_evidence',
] as const;

export function unassessedRisks(risk: RiskAssessment): Array<(typeof RISK_BOOL_FIELDS)[number]> {
  return RISK_BOOL_FIELDS.filter((field) => risk[field] === null);
}

// ─── status / evidence predicates ──────────────────────────────────────────
// Mirrors RootCauseStatus.is_actionable (models.py:140), EvidenceStance
// .is_evidence/.is_gap (models.py:96,110).

export function isActionable(status: RootCauseStatus): boolean {
  return status === 'confirmed' || status === 'probable';
}

const EVIDENCE_STANCES: readonly EvidenceStance[] = ['supports', 'contradicts', 'checked_absent'];

export function isEvidence(stance: EvidenceStance): boolean {
  return EVIDENCE_STANCES.includes(stance);
}

export function isGap(stance: EvidenceStance): boolean {
  return !isEvidence(stance);
}

// Mirrors MemoryStatus.usable_for_ranking (models.py:184).
export function usableForRanking(status: MemoryStatus): boolean {
  return status === 'verified' || status === 'trusted';
}

// ─── selected / rejected / tied hypotheses ─────────────────────────────────
// Mirrors Investigation.selected/.rejected (models.py:1126,1139).

export function selectedMatrix(investigation: Investigation | null | undefined): EvidenceMatrix | null {
  if (!investigation || investigation.matrices.length === 0) return null;
  if (!investigation.selected_hypothesis_id) return investigation.matrices[0];
  return (
    investigation.matrices.find((m) => m.hypothesis.hypothesis_id === investigation.selected_hypothesis_id) ??
    investigation.matrices[0]
  );
}

export function rejectedMatrices(investigation: Investigation | null | undefined): EvidenceMatrix[] {
  if (!investigation) return [];
  const chosen = selectedMatrix(investigation);
  return investigation.matrices.filter((m) => m !== chosen);
}

// For the UNCERTAIN two-candidate view: the top two matrices when the
// investigation did NOT discriminate between them (discriminated === false).
export function tiedCandidates(investigation: Investigation | null | undefined): EvidenceMatrix[] {
  if (!investigation || investigation.discriminated) return [];
  return investigation.matrices.slice(0, 2);
}

// ─── change events ──────────────────────────────────────────────────────────
// The free, always-present source of "what changed" — investigation.timeline
// events flagged is_change. The richer ChangeContext (api.correlate) is a
// separate, second-call enrichment (see ChangesTab).

export function changeEvents(investigation: Investigation | null | undefined) {
  if (!investigation) return [];
  return investigation.timeline.events.filter((e) => e.is_change);
}
