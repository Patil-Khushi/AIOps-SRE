"""Incident-history retrieval backends.

Each implements ``IncidentHistoryProvider``. Importing a provider module has no
side effects — the retriever decides which tiers exist and in what order, so an
import can never change retrieval behaviour on its own.
"""
