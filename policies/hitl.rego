# HITL policy — source of truth for autonomy levels per Solution Design slide 10.
#
# Phase 0: this file is reference-only. The in-process gate at aiops/policy/gate.py
# is the runtime check. Phase 2 wires OPA into the gate so this file becomes
# authoritative.
#
# Levels:
#   none      — fully autonomous
#   optional  — agent acts by default; tenant can switch on a human gate per policy
#   required  — human approval mandatory
#
# Keys are tool capabilities (see aiops/tools/registry.py).

package aiops.hitl

default level := "optional"

# Phase: Reactive-Active
level := "none"     if input.action == "observability.metrics.query"
level := "none"     if input.action == "observability.logs.query"
level := "none"     if input.action == "observability.traces.query"
level := "none"     if input.action == "notify.send"
level := "optional" if input.action == "itsm.incident.create"
level := "optional" if input.action == "itsm.incident.update"
level := "required" if input.action == "automation.runbook.execute"
level := "optional" if input.action == "chatops.war_room.create"

# RA-004 Runbook Executor: preview + non-destructive steps are autonomous;
# only the destructive execute path above is human-gated.
level := "none"     if input.action == "automation.runbook.simulate"
level := "none"     if input.action == "automation.runbook.apply"

# Clearing an injected demo fault. An INNER hop only: every route to it
# (rca.fix_step.execute, automation.runbook.execute) is already "required", so
# the human has approved upstream. Gating it again would refuse that approved
# call, because the inner dispatch forwards no approval context. It can also
# only ever recover — the provider refuses any target but "off".
level := "none"     if input.action == "automation.fault.clear"

# Phase: Proactive
level := "none"     if input.action == "topology.discover"
level := "optional" if input.action == "anomaly.report"
level := "optional" if input.action == "drift.report"
level := "optional" if input.action == "toil.report"

# Phase: Predictive
level := "required" if input.action == "capacity.recommend"
level := "required" if input.action == "slo.freeze_changes"
level := "required" if input.action == "change.predict_risk"
level := "optional" if input.action == "reliability.forecast"

# Phase: Prescriptive-Adaptive
level := "required" if input.action == "remediation.recommend"
level := "optional" if input.action == "auto_heal.execute"
level := "required" if input.action == "policy.optimize"
level := "required" if input.action == "feedback.promote_model"
level := "optional" if input.action == "cost.scale"
level := "required" if input.action == "knowledge.publish"
level := "required" if input.action == "chaos.experiment.run"

# RCA Agent — every fix step is required-HITL (Solution Design slide 10, slide 13).
level := "required" if input.action == "rca.fix_step.execute"

# Resolution verifier → ticket closure is human-gated (PRS-007 step 2).
level := "required" if input.action == "itsm.ticket.close"

# Convenience: a boolean for the gate code path that just wants "must I block?".
must_block_unattended if level == "required"

must_block_unattended if {
	level == "optional"
	input.tenant.requires_hitl == true
}

# Convenience: surface the reason text the gate puts in audit logs.
reason := sprintf("action=%s level=%s tenant=%v", [input.action, level, input.tenant.id])
