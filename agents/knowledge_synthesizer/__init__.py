"""Knowledge Synthesizer agent (PRS-007).

Turns a resolved incident (closed ticket + RCA output) into searchable
knowledge — a postmortem, a runbook suggestion, and a KB article — with a
platform-enforced human review before anything is published.

This package is built incrementally (additive-first). See the agent's
README for the per-checkpoint contract.
"""

from __future__ import annotations
