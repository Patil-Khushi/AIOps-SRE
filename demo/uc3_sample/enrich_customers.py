# Databricks notebook source
# ---------------------------------------------------------------------------
# SAMPLE (synthetic) CHILD notebook — customer enrichment. ~330 min.
# Anti-patterns: no column pruning, a shuffle join where a broadcast would do,
# and a reused DataFrame that is recomputed instead of cached.
# ---------------------------------------------------------------------------

from pyspark.sql import functions as F

staging = dbutils.widgets.get("staging")

# Anti-pattern: read every column of a very wide table when only a handful are
# used downstream (no column pruning / predicate pushdown).
orders = spark.read.parquet(staging)

# Small dimension table (~5 MB) — perfect broadcast candidate.
customers = spark.read.parquet("abfss://ref@datalake.dfs.core.windows.net/dim_customer/")

# Anti-pattern: plain join triggers a full shuffle of the large orders table
# against a tiny dimension. A broadcast join would avoid the shuffle entirely.
enriched = orders.join(customers, on="customer_id", how="left")

# Anti-pattern: `enriched` is used twice below but never cached, so the whole
# join is recomputed for each action.
active = enriched.filter(F.col("status") == "ACTIVE")
churned = enriched.filter(F.col("status") == "CHURNED")

active.write.mode("overwrite").parquet(staging + "active/")
churned.write.mode("overwrite").parquet(staging + "churned/")
