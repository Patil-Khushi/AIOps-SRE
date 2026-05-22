"""RCA Agent (PRS-008 ★) — the catalog's headline differentiator.

v0 scope (DEMO_PLAN.md, locked):
    Single scenario `slow-product-catalog`, single prompt template, structured
    RCAVerdict with ranked fix steps that carry blast_radius + rollback. Every
    fix step is tagged ``requires_hitl=true`` — the platform HITL gate is what
    blocks downstream execution; the agent does not gate-check itself.

Out of scope for v0: retrieval phase, safety.py command allow-list, multi-
scenario coverage, fix-step execution. Those are W2+ / post-POC.
"""
