"""Stages 10-12 — recovery planning, risk assessment, and the verification plan.

Turning a failure *class* into a runnable action
-----------------------------------------------
The catalog deliberately produces generic hypotheses: ``dependency_unavailable`` with an
``action_category`` of ``restore_dependency``. That is a remediation *class*, not
something an executor can run, and the gap between the two is where a confidently wrong
button gets made.

:func:`match_action_key` closes it by matching the hypothesis against the **runtime
vocabulary** — the keys the platform says it can execute — on shared tokens. So
``dependency_unavailable`` on component "Redis (payment-service)" finds a key containing
``redis`` for that service, and finds nothing when no such action exists. There is no
class→key table anywhere: a table would be a second copy of the platform's capabilities,
maintained by hand, and it would be the hardcoded failure-key list arriving through a
different door.

When no key matches, the option is ``manual``. That is the correct outcome, not a
degraded one — most real remediations are not one-click.

``grounded`` and ``executable`` are different questions
-------------------------------------------------------
``grounded`` asks "is this a real action key?". ``executable`` asks "and can the platform
run it right now?". They come apart precisely offline: the static map can confirm a key
is real while no executor is registered to run it. Keeping them separate is what lets the
dashboard show an honest "known fix, no executor here" instead of a button that fails
after approval.

Risk is assessed from the action's own shape
--------------------------------------------
:func:`assess_risk` answers the seven questions on ``RiskAssessment`` from what the
action *is*, and leaves the rest ``None``. Tri-state throughout: ``None`` means "not
assessed", and a risk register full of ``False`` that nobody evaluated is how a dangerous
action gets approved. ``unassessed`` counts them so the operator sees the gap.
"""

from __future__ import annotations

import re

from agents.rca_agent.investigation.models import (
    EvidenceMatrix,
    Hypothesis,
    RecoveryOption,
    RiskAssessment,
    VerificationPlan,
)
from agents.rca_agent.models import BlastRadius

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Tokens too common across both hypothesis names and action keys to discriminate. Without
# this, "service" alone matches every key for every service and the first key in the list
# always wins — which looks like successful grounding and is a coin flip.
_STOPWORDS = frozenset(
    {
        "service",
        "the",
        "and",
        "for",
        "with",
        "from",
        "unavailable",
        "down",
        "high",
        "error",
        "failure",
        "failed",
    }
)

MIN_KEY_MATCH = 1
"""Shared discriminating tokens required before a key is accepted for a hypothesis.

One is enough *because stopwords are removed first*: a surviving shared token like
``redis``, ``cpu``, ``memory`` or ``timeout`` names the condition. Requiring two would
reject ``redis_down`` ↔ "Redis (payment-service)", which is the clearest match there is.
"""


def _tokens(*values: str) -> set[str]:
    out: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall((value or "").lower()):
            if len(token) >= 3 and token not in _STOPWORDS:
                out.add(token)
    return out


def match_action_key(hypothesis: Hypothesis, vocabulary: tuple[str, ...]) -> tuple[str | None, str]:
    """Best runtime action key for one hypothesis, with the reason.

    Returns ``(key, rationale)``; ``key`` is ``None`` when nothing matches well enough,
    and the rationale says which tokens decided it — so a wrong match is reviewable
    rather than mysterious.

    Ties break on the key's own name so the choice is reproducible: an eval diff must
    not report a change that is really just dict ordering.
    """
    if not vocabulary:
        return None, "no executable action vocabulary was available"

    wanted = _tokens(hypothesis.candidate_component or "", hypothesis.category)
    if not wanted:
        return None, "the hypothesis carries no discriminating token to match on"

    scored: list[tuple[int, str, set[str]]] = []
    for key in vocabulary:
        # Only the condition half of "<service>.<condition>" discriminates; the service
        # half is already fixed by the caller scoping the vocabulary to one service.
        condition = key.split(".", 1)[-1]
        shared = wanted & _tokens(condition)
        if len(shared) >= MIN_KEY_MATCH:
            scored.append((len(shared), key, shared))

    if not scored:
        return None, (
            f"no action key shares a condition token with this hypothesis "
            f"(looked for {', '.join(sorted(wanted))})"
        )

    scored.sort(key=lambda row: (-row[0], row[1]))
    best_count, best_key, best_shared = scored[0]

    # An ambiguous match is not a match. Two keys tied on the same evidence means the
    # tokens did not choose, and picking the alphabetically-first one would be a guess
    # presented as a grounded action.
    tied = [row for row in scored if row[0] == best_count]
    if len(tied) > 1:
        return None, (
            f"{len(tied)} action keys match equally well "
            f"({', '.join(row[1] for row in tied)}) — the tokens did not discriminate, "
            "so no automated action is proposed"
        )

    return best_key, f"matched on {', '.join(sorted(best_shared))}"


def build_recovery_options(
    matrices: tuple[EvidenceMatrix, ...],
    *,
    vocabulary: tuple[str, ...],
    executor_available: bool,
    limit: int = 2,
) -> tuple[RecoveryOption, ...]:
    """One recovery option per top-ranked hypothesis, best first.

    Bounded to ``limit`` because an operator reading a page needs the leading candidate
    and its alternative, not a catalogue. Every option carries ``requires_hitl=True`` by
    type — the field is ``Literal[True]``, so an option that bypasses approval cannot be
    constructed at all rather than being caught by a later check.
    """
    options: list[RecoveryOption] = []
    claimed: dict[str, str] = {}
    for rank, matrix in enumerate(matrices[: max(1, limit)], start=1):
        hypothesis = matrix.hypothesis
        key, rationale = match_action_key(hypothesis, vocabulary)
        score = matrix.score.score if matrix.score else 0.0

        # One action, one option. Two hypotheses can legitimately match the same key —
        # "the store is unreachable" and "requests are failing with a store error" are two
        # readings of one failure — and offering it twice puts two identical approve
        # buttons on the screen. The higher-ranked hypothesis keeps the action; the rival
        # becomes a manual step that says why.
        if key and key in claimed:
            rationale = (
                f"the same action was already proposed for the higher-ranked "
                f"{claimed[key]} hypothesis, so there is no second action to offer"
            )
            key = None
        elif key:
            claimed[key] = hypothesis.category

        if key:
            description = f"Clear the {key} condition on {hypothesis.candidate_component or 'the affected service'}."
            effect = "the failing condition is removed and the service should recover"
        else:
            description = (
                f"Investigate and remediate manually: {hypothesis.mechanism} "
                f"({(hypothesis.action_hint or 'no action class').replace('_', ' ')})."
            )
            effect = "unknown until a human establishes the remediation"

        options.append(
            RecoveryOption(
                option_id=f"recovery-{rank}",
                description=description,
                addresses_hypothesis_id=hypothesis.hypothesis_id,
                why_it_addresses_the_cause=(
                    f"the ranked cause is {hypothesis.category} (score {score:.2f}); {rationale}"
                ),
                expected_effect=effect,
                rollback=(
                    f"re-apply the {key} condition" if key else "no automated change to reverse"
                ),
                blast_radius=BlastRadius.LOW if key else BlastRadius.MEDIUM,
                confidence=score,
                action_key=key,
                # Grounded: the key came from the runtime vocabulary, so it is real.
                grounded=key is not None,
                # Executable: real *and* something is registered to run it. False offline
                # even for a valid key, which is the honest answer there.
                executable=bool(key) and executor_available,
                risk=assess_risk(action_key=key, hypothesis=hypothesis),
            )
        )
    return tuple(options)


# Conditions whose remediation restarts or replaces the process. Matched on the action
# key's condition half, so the judgement follows the action rather than the diagnosis.
_RESTART_CONDITIONS = ("crashloop", "oom", "memory_leak", "memory_exhaust")
_DEPENDENCY_CONDITIONS = ("mysql", "postgres", "redis", "down", "dns")


def assess_risk(*, action_key: str | None, hypothesis: Hypothesis) -> RiskAssessment:
    """Answer what the action's shape supports, and leave the rest unassessed.

    Every field is tri-state. ``None`` is not a placeholder here — it is the accurate
    answer for a question this stage cannot settle, and the alternative (defaulting to
    ``False``) manufactures a clean risk register that nobody checked. ``concerns``
    carries the answers in prose for the approval screen.
    """
    if action_key is None:
        return RiskAssessment(
            level="unknown",
            # ``reversible`` and ``rollback_available`` are strict ``bool`` while the seven
            # risk *questions* are tri-state, and that asymmetry is deliberate:
            # reversibility is a requirement the platform must be able to state, not an
            # open question (CLAUDE.md #5 — every action reversible, and the reverse
            # tested). A manual step changes nothing, so it is trivially reversible and
            # there is nothing to roll back. Saying ``True``/``False`` here is a claim
            # about *this step*, not a prediction about whatever the operator later does.
            reversible=True,
            rollback_available=False,
            concerns=("no automated action is proposed, so a human decides and owns the risk",),
            rationale=(
                "manual remediation: this step makes no change, so the risk is whatever the "
                "operator chooses to do next and is not assessed here"
            ),
        )

    condition = action_key.split(".", 1)[-1].lower()
    restarts = any(needle in condition for needle in _RESTART_CONDITIONS)
    dependency = any(needle in condition for needle in _DEPENDENCY_CONDITIONS)

    concerns: list[str] = []
    if restarts:
        concerns.append(
            "clearing this condition is expected to restart or replace the process, so "
            "in-flight requests on that instance are lost"
        )
    if dependency:
        concerns.append(
            "restoring a datastore affects every caller of it, not only the alerting service"
        )

    return RiskAssessment(
        # MEDIUM whenever requests are interrupted; LOW otherwise. Never HIGH from this
        # evidence: HIGH should mean data loss or irreversibility, and nothing here
        # establishes either.
        level="medium" if restarts else "low",
        interrupts_active_requests=True if restarts else None,
        causes_downtime=None,
        risks_data_loss=None,
        risks_duplicate_transactions=None,
        affects_downstream=True if dependency else None,
        affects_upstream=None,
        destroys_evidence=True if restarts else None,
        reversible=True,
        rollback_available=True,
        concerns=tuple(concerns),
        rationale=(
            f"assessed from the action ({action_key}) rather than from the diagnosis; "
            "unanswered questions are reported as unassessed, not as safe"
        ),
    )


DEFAULT_RECHECK_OFFSETS = (60, 180, 300)
"""Staged re-check offsets, in seconds after the action.

Copied from ``resolution_verifier``'s ``VERIFIER_WINDOW_SECONDS`` default rather than
chosen here: RCA writes the plan and that agent executes it, so a second cadence would
mean the plan and the verifier disagreed about when "not resolved" is established. A
single window would also mis-read a slow recovery as a failure.
"""


def build_verification_plan(
    matrices: tuple[EvidenceMatrix, ...],
    *,
    recheck_offsets: tuple[int, ...] = DEFAULT_RECHECK_OFFSETS,
) -> VerificationPlan:
    """What to re-check after a fix, derived from what established the cause.

    The checks are the *supporting evidence read back*: the observation that proved the
    problem is the one that proves the recovery. Inventing separate success criteria
    would let a fix "pass" verification without touching the signal that raised the
    incident.
    """
    if not matrices:
        return VerificationPlan(
            checks=(),
            success_criteria=("no cause was established, so there is nothing to verify",),
            window_seconds=recheck_offsets,
            if_not_resolved="re-investigate from the alert",
        )

    top = matrices[0]
    checks = tuple(item.statement for item in top.supporting[:4])
    return VerificationPlan(
        checks=checks or ("re-read the service's telemetry",),
        success_criteria=(
            "every observation that established the cause has returned to normal",
            "a partial recovery counts as NOT resolved — the verifier must not pass a "
            "fix that improved the symptom without clearing it",
        ),
        window_seconds=recheck_offsets,
        if_not_resolved=(
            "roll back the applied action and re-investigate — the ranked cause was "
            f"{top.hypothesis.category}, so its rivals are the next candidates"
        ),
    )


__all__ = [
    "DEFAULT_RECHECK_OFFSETS",
    "MIN_KEY_MATCH",
    "assess_risk",
    "build_recovery_options",
    "build_verification_plan",
    "match_action_key",
]
