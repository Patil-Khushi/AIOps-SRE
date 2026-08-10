"""Shared incident corpus — the population every history provider searches.

Two truth-file families exist in this repo and they describe **different
applications**, which is why retrieval kept returning nothing:

* ``demo/truth_files/*.yaml`` — the upstream OpenTelemetry Astronomy Shop
  (``ad``, ``cart``, ``currency``, ``kafka``, ``recommendation``). 15 incidents,
  written against flagd flags that are no longer deployed.
* ``demo/ecommerce/truth_files/*.json`` — the ecommerce SUT actually running
  here (``user-service``, ``order-service``, ``payment-service``, and the
  mysql/postgres/redis stores behind them). 12 incidents.

``MockIncidentHistoryProvider`` reads only the first, so every correlation of a
*live* incident scored against a corpus about a different system and fell below
the similarity floor. That is correct behaviour on the wrong population, not a
scoring bug — and no amount of tuning the weights would have fixed it.

Both families are loaded here, normalised to one record shape, so a provider
searches the whole recorded history rather than whichever half its loader
happened to understand.

Deliberately kept as plain dicts rather than a pydantic model: providers map
these into ``IncidentMatch`` themselves, and a second schema in between would
only add a translation layer with no validation value.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Overridable so a test can point at a fixture dir, matching the mock's seam.
OTEL_TRUTH_DIR = Path(
    os.environ.get("AIOPS_TRUTH_FILES_DIR", str(_REPO_ROOT / "demo" / "truth_files"))
)
ECOMMERCE_TRUTH_DIR = Path(
    os.environ.get(
        "AIOPS_ECOMMERCE_TRUTH_FILES_DIR",
        str(_REPO_ROOT / "demo" / "ecommerce" / "truth_files"),
    )
)


def _clean(items: Any) -> list[str]:
    """Coerce a loosely-typed YAML/JSON field into a list of non-empty strings."""
    if not items:
        return []
    if isinstance(items, str):
        return [items.strip()] if items.strip() else []
    out: list[str] = []
    for i in items:
        s = str(i).strip()
        if s:
            out.append(s)
    return out


def _first_fix_description(data: dict) -> str | None:
    """First *fix step* from an OTel truth file — what was done, not what was guessed.

    Mirrors the mock provider's helper. Sourcing this from ``ranked_hypotheses[0]``
    would relabel a proposed cause as a settled resolution, and since this corpus
    is handed to the RCA agent as precedent, one incident's speculation would
    become another's evidence.
    """
    steps = data.get("expected_fix") or data.get("fix_steps") or []
    if isinstance(steps, dict):
        steps = [steps]
    for step in steps or []:
        if isinstance(step, dict):
            desc = str(step.get("description") or step.get("action") or "").strip()
            if desc:
                return desc
        elif str(step).strip():
            return str(step).strip()
    return None


def _load_otel_yaml(directory: Path) -> list[dict]:
    """Parse the Astronomy Shop YAML truth files."""
    corpus: list[dict] = []
    if not directory.is_dir():
        logger.debug("incident corpus: otel truth dir %s not found", directory)
        return corpus
    try:
        import yaml
    except Exception:  # pragma: no cover - pyyaml ships with the repo's deps
        logger.debug("incident corpus: pyyaml unavailable")
        return corpus

    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "template":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            # Tolerant by design: history is an enrichment and a malformed file
            # must degrade the corpus, never cost a correlation.
            logger.debug("incident corpus: skipping %s (%s)", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue

        cause = data.get("real_cause") or {}
        rca = data.get("expected_rca") or {}
        component = str(cause.get("component") or "").strip().lower()
        corpus.append(
            {
                "incident_id": str(data.get("scenario_id") or path.stem),
                "title": data.get("title"),
                "occurred_at": data.get("last_updated"),
                "signatures": _clean(rca.get("evidence_signals")),
                "services": [component] if component else [],
                "topology": [component] if component else [],
                "recorded_cause": cause.get("description") or rca.get("cause_summary"),
                "resolution_summary": _first_fix_description(data),
                "owner": data.get("owner"),
                "source": "otel-demo",
            }
        )
    return corpus


def _load_ecommerce_json(directory: Path) -> list[dict]:
    """Parse the ecommerce SUT JSON truth files.

    A different schema from the YAML family, so it needs its own mapping rather
    than a shared one: there is no ``real_cause`` block, the cause is a flat
    ``root_cause`` string, and the signals live under ``expected_signals`` plus
    the alertname on ``expected_alert_payload``.
    """
    corpus: list[dict] = []
    if not directory.is_dir():
        logger.debug("incident corpus: ecommerce truth dir %s not found", directory)
        return corpus

    for path in sorted(directory.glob("*.json")):
        if path.stem.startswith("_") or path.stem == "template":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("incident corpus: skipping %s (%s)", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue

        service = str(data.get("service") or "").strip()
        signals = data.get("expected_signals") or {}

        # Signatures are assembled from every recorded identifier of "what went
        # wrong": the alert that fires, the fault taxonomy, the cause keywords,
        # and the metric names. Keyword scoring needs the discrete tokens and
        # embedding scoring needs the prose, so both are included.
        signatures: list[str] = []
        alertname = ((data.get("expected_alert_payload") or {}).get("labels") or {}).get(
            "alertname"
        )
        if alertname:
            signatures.append(str(alertname))
        if data.get("fault_category"):
            signatures.append(str(data["fault_category"]))
        signatures.extend(_clean(data.get("root_cause_keywords")))
        for group in ("metrics", "container", "logs"):
            for entry in signals.get(group) or []:
                if isinstance(entry, dict):
                    name = entry.get("name") or entry.get("signal")
                    if name:
                        signatures.append(str(name))
                elif str(entry).strip():
                    signatures.append(str(entry).strip())

        corpus.append(
            {
                "incident_id": str(data.get("id") or path.stem),
                "title": data.get("root_cause") or f"{service} incident",
                # These files carry no date. None rather than a fabricated
                # timestamp — an invented occurred_at would make a synthetic
                # corpus look like real operational history.
                "occurred_at": None,
                "signatures": signatures,
                "services": [service] if service else [],
                "topology": [service] if service else [],
                "recorded_cause": data.get("root_cause"),
                "resolution_summary": data.get("remediation"),
                "owner": None,
                "source": "ecommerce",
            }
        )
    return corpus


_CORPUS: list[dict] | None = None


def load_corpus() -> list[dict]:
    """Every recorded incident from both truth-file families.

    Cached: the files do not change during a process, and parsing on import would
    make ``import aiops.tools`` do disk I/O.
    """
    global _CORPUS
    if _CORPUS is None:
        combined = _load_otel_yaml(OTEL_TRUTH_DIR) + _load_ecommerce_json(ECOMMERCE_TRUTH_DIR)
        # Later families win on id collision, but log it — two truth files
        # claiming one incident id is a repo problem worth seeing.
        seen: dict[str, dict] = {}
        for rec in combined:
            if rec["incident_id"] in seen:
                logger.warning(
                    "incident corpus: duplicate incident_id %r; keeping the later record",
                    rec["incident_id"],
                )
            seen[rec["incident_id"]] = rec
        _CORPUS = list(seen.values())
        logger.info(
            "incident corpus: %d incident(s) loaded (otel=%s, ecommerce=%s)",
            len(_CORPUS),
            OTEL_TRUTH_DIR,
            ECOMMERCE_TRUTH_DIR,
        )
    return _CORPUS


def reset_corpus_for_tests() -> None:
    """Test seam — the cache is module state, so a test pointing at a different
    truth dir would otherwise get the previous corpus."""
    global _CORPUS
    _CORPUS = None
