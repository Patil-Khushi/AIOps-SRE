# Databricks notebook source
# ---------------------------------------------------------------------------
# SAMPLE (synthetic) parent notebook for UC3 — an Azure Databricks incremental
# load orchestrator. NOT client code. Used so the perf_reliability agent has a
# realistic, offline job to analyze before live Databricks access is available.
# ---------------------------------------------------------------------------

in_path = "abfss://raw@datalake.dfs.core.windows.net/orders/"
staging_path = "abfss://staging@datalake.dfs.core.windows.net/orders/"
out_path = "abfss://curated@datalake.dfs.core.windows.net/orders_agg/"

# Full reload every run — even though only yesterday's partition changed.
# (Opportunity: switch to an incremental/merge on the watermark column.)
orders = spark.read.parquet(in_path)
orders.write.mode("overwrite").parquet(staging_path)

# Hand off the heavy aggregation to the child notebook.
dbutils.notebook.run("transforms_child", timeout_seconds=7200, arguments={"staging_path": staging_path, "out_path": out_path})
