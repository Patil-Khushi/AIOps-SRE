"""Incident Classifier agent (RA-002) — Reactive-Active phase.

Owns the second stage of the alert→incident pipeline: classify an already-
triaged incident into one of five types so downstream routing (RA-003
Auto-Ticketing, runbook selection, RCA) can pick the right path.

Public surface::

    from agents.incident_classifier import (
        AuditMetadata,
        Classification,
        ClassificationInput,
        IncidentType,
        classify,
    )
"""

from agents.incident_classifier.agent import classify
from agents.incident_classifier.models import (
    AuditMetadata,
    Classification,
    ClassificationInput,
    IncidentType,
)

__all__ = [
    "AuditMetadata",
    "Classification",
    "ClassificationInput",
    "IncidentType",
    "classify",
]
