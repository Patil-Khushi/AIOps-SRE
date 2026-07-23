"""Databricks tool providers (UC3 client track).

Registers the ``code.assets.fetch`` capability — "give me a job's notebook
source + per-asset runtimes." Two providers live here:

- ``sample`` (shipped): reads a bundled sample job from ``demo/uc3_sample/`` so
  the UC3 agent runs end-to-end with no Azure access. This is the demo default.
- ``databricks`` (TODO — Person B): the live provider hitting the Databricks
  Jobs API (run history / task timings) + Workspace API (notebook source).
  Register it here and call ``get_registry().select_provider(...)`` to swap.

Importing this package registers the providers (the ``@tool`` decorator runs on
import), so callers do ``import aiops.tools.databricks`` once at a wiring point.
"""

from aiops.tools.databricks import sample_provider  # import triggers registration

__all__ = ["sample_provider"]
