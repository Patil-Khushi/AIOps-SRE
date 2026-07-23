# Databricks notebook source
# ---------------------------------------------------------------------------
# SAMPLE (synthetic) CHILD notebook — final curated write. ~300 min.
# Anti-pattern: coalesce(1) serializes the entire write onto one task, and the
# output is unpartitioned so downstream reads can't prune. NOT client code.
# ---------------------------------------------------------------------------

staging = dbutils.widgets.get("staging")
curated = dbutils.widgets.get("curated")

sales_agg = spark.read.parquet(staging + "sales_agg/")

# Anti-pattern #1: coalesce(1) collapses the write to a single executor task —
# the whole curated write serializes. Anti-pattern #2: no partitionBy, so the
# curated dataset is one big blob that downstream queries can't prune by date.
sales_agg.coalesce(1).write.mode("overwrite").parquet(curated)
