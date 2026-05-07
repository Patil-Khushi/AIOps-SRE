"""Hand-rolled JSON eval harness — minimum viable from day one.

POC guide §9.5: "When you build an agent, build its eval set in the same week."
Score every PR; fail the build if pass rate drops more than 2 percentage points
versus ``main``. This module is the floor; switch to Ragas/DeepEval/LangSmith
later only if the case count gets unwieldy.
"""
