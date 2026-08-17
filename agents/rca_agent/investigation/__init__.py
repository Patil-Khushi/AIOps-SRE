"""Deterministic investigation stages for the RCA Agent (PRS-008).

The RCA agent used to be one shape: evidence in, one LLM call, root cause out.
That makes the model responsible for everything — which hypotheses exist, which
evidence bears on them, what contradicts them, how confident to be — and the only
record of that reasoning is prose it wrote about itself.

This package is the other half: the part a production SRE does *before* forming a
conclusion, expressed as deterministic Python over an already-collected
``IncidentContext``.

    scope -> timeline -> baseline -> completeness -> memory
          -> hypotheses -> evidence matrix -> scoring
          -> blast radius -> recovery/risk
          -> ONE LLM call, which explains the result rather than deciding it

Phase 1 lands the data contracts only (``models.py``). Every stage module arrives
in a later phase against these types, so the shapes the LLM prompt, the dashboard,
the eval harness and the memory store all depend on are agreed before any logic is
written against them.

Why here rather than in ``aiops/``
---------------------------------
These are RCA's reasoning vocabulary, not platform primitives. ``aiops/context/``
deliberately refuses to hold "agent-specific projection" for the same reason
(see its module docstring): a hypothesis, a causal chain and a recovery risk
assessment encode how *this* agent investigates. The platform owns evidence; the
agent owns what the evidence means.
"""
