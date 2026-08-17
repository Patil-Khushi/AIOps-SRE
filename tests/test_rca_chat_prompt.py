"""Two-sided ratchet on RCA_CHAT_SYSTEM_PROMPT_V1 — the same style as
tests/test_rca_prompt_v7.py: what must stay OUT (checked against V7's own
fixtures, since the chat prompt must never regain what V7 had to lose) and
what must stay IN (the clauses that make the chat safe to ship)."""

from __future__ import annotations

import pytest

from agents.rca_agent.prompts import (
    RCA_CHAT_GROUNDING_BLOCK,
    RCA_CHAT_SYSTEM_PROMPT_V1,
    RCA_CHAT_USER_V1,
    SYSTEM_PROMPT_V6,
)
from tests.test_rca_prompt_v7 import ALL_FAULT_KEYS, INJECTION_MECHANISMS


class TestNoInjectionTruth:
    @pytest.mark.parametrize("mechanism", INJECTION_MECHANISMS)
    def test_no_injection_mechanism_survives(self, mechanism):
        assert mechanism not in RCA_CHAT_SYSTEM_PROMPT_V1

    @pytest.mark.parametrize("key", ALL_FAULT_KEYS)
    def test_no_failure_key_is_hardcoded(self, key):
        assert key not in RCA_CHAT_SYSTEM_PROMPT_V1

    def test_the_alert_to_key_mapping_is_gone(self):
        assert "DISAMBIGUATION" not in RCA_CHAT_SYSTEM_PROMPT_V1

    def test_positive_control_v6_really_contains_it(self):
        """Without this, the assertions above could pass because the strings
        were never going to be there — proving the check isn't vacuous."""
        for mechanism in INJECTION_MECHANISMS:
            assert mechanism in SYSTEM_PROMPT_V6, mechanism
        for key in ALL_FAULT_KEYS:
            assert key in SYSTEM_PROMPT_V6, key


class TestRequiredSafetyClauses:
    @pytest.mark.parametrize(
        "clause",
        [
            "FROZEN",
            "final for THIS conversation",
            "HONEST ABSTENTION",
            "CITATIONS ARE MANDATORY",
            "CANNOT EXECUTE",
            "CANNOT RE-INVESTIGATE",
            "UNTRUSTED DATA",
            "INPUT HANDLING",
            "answerable",
            "suggested_actions",
            "EVIDENCE CATEGORIES ARE NOT INTERCHANGEABLE",
            "CURRENT INCIDENT EVIDENCE OUTRANKS HISTORICAL RAG",
            "HISTORICAL — NOT CURRENT EVIDENCE",
        ],
    )
    def test_clause_present(self, clause):
        assert clause in RCA_CHAT_SYSTEM_PROMPT_V1

    def test_it_forbids_presenting_a_historical_fix_as_the_current_fix(self):
        assert 'never "the fix for this incident is X"' in RCA_CHAT_SYSTEM_PROMPT_V1

    def test_it_forbids_restating_a_different_number_or_cause(self):
        assert "may NOT restate a different confidence number" in RCA_CHAT_SYSTEM_PROMPT_V1
        assert "may NOT present a different cause" in RCA_CHAT_SYSTEM_PROMPT_V1

    def test_it_requires_json_only(self):
        assert "Reply with ONE JSON object" in RCA_CHAT_SYSTEM_PROMPT_V1

    def test_grounding_and_user_blocks_render_without_a_stray_format_field(self):
        rendered = RCA_CHAT_GROUNDING_BLOCK.format(pack="x")
        assert "{" not in rendered
        rendered = RCA_CHAT_USER_V1.format(question="y")
        assert "{" not in rendered

    def test_grounding_and_user_blocks_are_marked_untrusted(self):
        assert "untrusted data" in RCA_CHAT_GROUNDING_BLOCK.lower()
        assert "untrusted data" in RCA_CHAT_USER_V1.lower()

    def test_it_is_not_chained_onto_v7(self):
        """A genuinely different prompt — not a .replace() link in the
        SYSTEM_PROMPT_V1..V7 chain, so an unrelated V7 edit can't reshape it."""
        from agents.rca_agent import prompts

        assert "RCA_CHAT_SYSTEM_PROMPT_V1" not in prompts.SYSTEM_PROMPT_V7
