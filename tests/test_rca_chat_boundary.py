"""The RCA chat's constraints, stated as controls rather than promises.

"The chat cannot execute anything, cannot pull new memory, cannot re-run the
pipeline, cannot read a truth file." A docstring saying so is not enforcement
— checked against the module's AST, the same discipline
tests/test_rca_learning.py::TestLearningBoundary and
tests/test_rca_memory_blindness.py use, for the same reason: a substring scan
over comments/docstrings would flag its own documentation instead of the code.

agents/rca_agent/incident_rag.py (the historical-incident RAG feature) gets
the SAME scan as chat.py/rca_chat_routes.py/investigation_context.py, with
one deliberate, narrow exception: it is the only one of the four allowed to
import aiops.tools.incident_history.providers.embedding (a read-only
model accessor, not a registry capability) and aiops.state (to read
persisted RCA verdicts) — everything else stays blocked for all four.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agents.rca_agent import chat, incident_rag, investigation_context
from demo.ui import rca_chat_routes

CHAT_SRC = pathlib.Path(chat.__file__).read_text(encoding="utf-8")
ROUTES_SRC = pathlib.Path(rca_chat_routes.__file__).read_text(encoding="utf-8")
CONTEXT_SRC = pathlib.Path(investigation_context.__file__).read_text(encoding="utf-8")
RAG_SRC = pathlib.Path(incident_rag.__file__).read_text(encoding="utf-8")

ALL_SRCS = (CHAT_SRC, ROUTES_SRC, CONTEXT_SRC, RAG_SRC)

# The ONE narrow exception to "no aiops.tools import" — a read-only accessor
# for an already-loaded embedding model, not a registry capability. Only
# incident_rag.py may reference it (checked below); the other three modules
# still may not import anything under aiops.tools at all.
_ALLOWED_TOOLS_IMPORTS = frozenset({"aiops.tools.incident_history.providers.embedding"})


def _referenced_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[-1])
    return names


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class TestChatCannotExecuteOrBypassHitl:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "get_registry",
            "get_gate",
            "enforce",
            "apply_rca_fix",
            "executeOption",
            "execute_option",
        ],
    )
    def test_chat_module_never_references_execution_surfaces(self, forbidden):
        assert forbidden not in _referenced_names(CHAT_SRC), (
            f"chat.py references {forbidden!r} — the chat may explain and cite, never execute"
        )

    @pytest.mark.parametrize(
        "forbidden",
        [
            "get_registry",
            "get_gate",
            "enforce",
            "apply_rca_fix",
            "executeOption",
            "execute_option",
        ],
    )
    def test_investigation_context_module_never_references_execution_surfaces(self, forbidden):
        assert forbidden not in _referenced_names(CONTEXT_SRC), (
            f"investigation_context.py references {forbidden!r} — this module is a "
            "read-only accessor over an already-frozen Investigation, nothing more"
        )

    @pytest.mark.parametrize(
        "forbidden",
        [
            "get_registry",
            "get_gate",
            "enforce",
            "apply_rca_fix",
            "executeOption",
            "execute_option",
        ],
    )
    def test_incident_rag_module_never_references_execution_surfaces(self, forbidden):
        assert forbidden not in _referenced_names(RAG_SRC), (
            f"incident_rag.py references {forbidden!r} — it may only search and read, "
            "the same posture as every other module in this boundary"
        )

    @pytest.mark.parametrize("forbidden", ["get_registry", "get_gate", "enforce"])
    def test_routes_module_never_references_execution_surfaces(self, forbidden):
        assert forbidden not in _referenced_names(ROUTES_SRC), (
            f"rca_chat_routes.py references {forbidden!r} — remediation must still "
            "go through POST /api/demo/rca/apply-fix, not this endpoint"
        )

    def test_it_imports_no_policy_module(self):
        for module in (
            _imported_modules(CHAT_SRC)
            | _imported_modules(ROUTES_SRC)
            | _imported_modules(CONTEXT_SRC)
            | _imported_modules(RAG_SRC)
        ):
            assert not module.startswith("aiops.policy"), module

    def test_it_imports_no_tools_module_except_the_one_allowed_rag_accessor(self):
        """Requirement: "the chat layer may access ONLY the new narrow
        read-only similar-incident search interface" — everything else under
        aiops.tools (the capability registry, execution seams) stays blocked
        for all four modules; only incident_rag.py may reach the one
        allowlisted read-only embedding accessor, and only that one."""
        for src, label in (
            (CHAT_SRC, "chat.py"),
            (ROUTES_SRC, "rca_chat_routes.py"),
            (CONTEXT_SRC, "investigation_context.py"),
            (RAG_SRC, "incident_rag.py"),
        ):
            for module in _imported_modules(src):
                if not module.startswith("aiops.tools"):
                    continue
                assert module in _ALLOWED_TOOLS_IMPORTS, (
                    f"{label} imports disallowed tools module {module!r}"
                )
                assert src is RAG_SRC, (
                    f"{label} is not allowed to import {module!r} — only incident_rag.py is"
                )

    @pytest.mark.parametrize("forbidden", ["register_provider", "register_env_adapters", "tool"])
    def test_no_module_ever_registers_a_tool(self, forbidden):
        """Phase 20 item 22: the chat layer must not be able to add a new
        capability to the platform's action registry — it only ever reads
        from what the registry already resolved at investigation time
        (baked into the frozen Investigation), never registers anything
        itself."""
        for src in ALL_SRCS:
            assert forbidden not in _referenced_names(src), forbidden


class TestChatCannotPullNewMemory:
    @pytest.mark.parametrize(
        "forbidden",
        ["incident_history", "memory", "OUTCOME_BACKED_PROVIDERS", "recall", "promote"],
    )
    def test_chat_module_never_references_the_memory_subsystem(self, forbidden):
        assert forbidden not in _referenced_names(CHAT_SRC), (
            f"chat.py references {forbidden!r} — historical influence already lives "
            "in the frozen Investigation; the chat must not perform its own recall"
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["incident_history", "memory", "OUTCOME_BACKED_PROVIDERS", "recall", "promote"],
    )
    def test_investigation_context_module_never_references_the_memory_subsystem(self, forbidden):
        assert forbidden not in _referenced_names(CONTEXT_SRC), (
            f"investigation_context.py references {forbidden!r} — historical influence "
            "already lives in the frozen Investigation.historical_influence field"
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["memory", "OUTCOME_BACKED_PROVIDERS", "recall", "promote", "record_verified_outcome"],
    )
    def test_incident_rag_module_never_references_rcas_own_memory_subsystem(self, forbidden):
        """incident_rag.py DOES reference "incident_history" (the module path
        of its one allowed import) so that name is deliberately excluded from
        this parametrize — everything RCA's confidence-affecting memory
        actually needs (memory.py, promote, OUTCOME_BACKED_PROVIDERS, recall,
        verified-outcome writes) stays out."""
        assert forbidden not in _referenced_names(RAG_SRC), forbidden

    def test_it_does_not_import_the_investigation_memory_module(self):
        imported = (
            _imported_modules(CHAT_SRC)
            | _imported_modules(CONTEXT_SRC)
            | _imported_modules(RAG_SRC)
        )
        assert "agents.rca_agent.investigation.memory" not in imported

    def test_it_does_not_import_the_learning_module(self):
        imported = (
            _imported_modules(CHAT_SRC)
            | _imported_modules(CONTEXT_SRC)
            | _imported_modules(RAG_SRC)
        )
        assert "agents.rca_agent.learning" not in imported


class TestChatCannotReadTruthFiles:
    @pytest.mark.parametrize("forbidden", ["truth_file", "truth_files"])
    def test_no_truth_file_reference(self, forbidden):
        for src in ALL_SRCS:
            assert forbidden not in src


class TestChatCannotReinvestigate:
    def test_chat_module_never_calls_analyze(self):
        assert "analyze" not in _referenced_names(CHAT_SRC), (
            "a follow-up turn must not be able to re-run the pipeline; a question "
            "needing new evidence gets answerable=False + a reanalyze suggestion, "
            "which starts a NEW run_id end-to-end, not a mutation of this one"
        )

    def test_routes_module_never_calls_analyze(self):
        assert "rca_analyze" not in _referenced_names(ROUTES_SRC)
        assert "analyze" not in _referenced_names(ROUTES_SRC)

    def test_investigation_context_module_never_calls_analyze(self):
        assert "analyze" not in _referenced_names(CONTEXT_SRC), (
            "the read-only context provider must not be able to trigger a fresh "
            "investigation either — it only ever renders what is already frozen"
        )

    def test_incident_rag_module_never_calls_analyze(self):
        assert "analyze" not in _referenced_names(RAG_SRC), (
            "the historical-incident search must not be able to trigger a fresh "
            "investigation — it only ever searches PAST, already-persisted verdicts"
        )


class TestIncidentRagIsReadOnly:
    """The specific new-feature boundary: a real search, but a hard wall away
    from anything that writes, promotes, or scores."""

    def test_it_never_calls_a_repository_write_function(self):
        forbidden_writes = {
            "save_rca_result",
            "save_verdict",
            "save_classification",
            "save_historical_incident",
            "upsert_cluster",
            "delete_all_rca_results",
        }
        assert not (forbidden_writes & _referenced_names(RAG_SRC)), (
            "incident_rag.py must only READ persisted verdicts (list_rca_results), never write"
        )

    def test_it_only_calls_list_rca_results_on_the_repository(self):
        assert "list_rca_results" in _referenced_names(RAG_SRC)
        assert "save_rca_result" not in _referenced_names(RAG_SRC)

    def test_it_never_references_confidence_or_root_cause_status_fields_as_writable(self):
        # A read-only search may READ these keys out of a persisted verdict's
        # dict (via .get("root_cause_status") etc, which are ast.Constant
        # string literals, not Name/Attribute nodes — so they never appear
        # here at all). This asserts the module has no *attribute* write path
        # onto a live RCAVerdict/Investigation object.
        assert "confidence_score" not in _referenced_names(RAG_SRC)


class TestChatAnswerHasNowhereToPutAVerdict:
    """Structural, not just a convention: the response model literally has no
    field the model's prose could populate to claim a new verdict."""

    def test_chat_answer_has_no_confidence_or_root_cause_field(self):
        fields = set(chat.ChatAnswer.model_fields)
        assert "confidence_score" not in fields
        assert "confidence" not in fields
        assert "root_cause" not in fields
        assert "root_cause_status" not in fields

    def test_chat_answer_forbids_extra_fields(self):
        assert chat.ChatAnswer.model_config.get("extra") == "forbid"

    def test_historical_incident_ref_has_no_confidence_or_root_cause_field_either(self):
        """The historical-incidents field carries its own narrow shape
        (incident_id/similarity/recorded_fix) — it cannot smuggle a verdict
        field in either."""
        fields = set(chat.HistoricalIncidentRef.model_fields)
        assert "confidence_score" not in fields
        assert "root_cause_status" not in fields
        assert chat.HistoricalIncidentRef.model_config.get("extra") == "forbid"

    def test_historical_incidents_are_never_parsed_from_the_models_json(self):
        """_coerce_answer (what turns the model's raw JSON into a ChatAnswer)
        must not read a "historical_incidents" key from the model's own
        response — that field is only ever attached server-side in answer(),
        from a real search, after the fact."""
        source = pathlib.Path(chat.__file__).read_text(encoding="utf-8")
        coerce_fn = source[source.index("def _coerce_answer") : source.index("def _validate")]
        assert "historical_incidents" not in coerce_fn


class TestPositiveControl:
    """Proves the AST check isn't vacuous — a source string that DOES
    reference a forbidden name must actually be caught."""

    def test_the_referenced_names_walk_catches_a_real_violation(self):
        bad_source = "from aiops.tools import get_registry\n\ndef f():\n    return get_registry()\n"
        assert "get_registry" in _referenced_names(bad_source)
        assert "aiops.tools" in _imported_modules(bad_source)
