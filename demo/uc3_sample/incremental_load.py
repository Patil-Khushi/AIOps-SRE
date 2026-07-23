# Databricks notebook source
# ---------------------------------------------------------------------------
# SAMPLE (synthetic) PARENT notebook — "orders_incremental_load" job orchestrator.
# Mirrors a real Azure Databricks incremental load that has drifted into a >24h
# runtime. NOT client code. Used so perf_reliability has a believable job to
# analyze offline / on the demo LLM.
# ---------------------------------------------------------------------------

from pyspark.sql import functions as F

RAW = "abfss://raw@datalake.dfs.core.windows.net/orders/"
STAGING = "abfss://staging@datalake.dfs.core.windows.net/orders/"
CURATED = "abfss://curated@datalake.dfs.core.windows.net/orders_curated/"

# Anti-pattern: FULL reload every run. The upstream only appends yesterday's
# partition, but we re-read and overwrite the entire history each night.
orders = spark.read.parquet(RAW)
orders.write.mode("overwrite").parquet(STAGING)

# Orchestrate the three child notebooks in sequence. Each is a separate asset
# with its own runtime — the child call tree the agent should drill into.
dbutils.notebook.run("enrich_customers", 7200, {"staging": STAGING})
dbutils.notebook.run("transforms_child", 14400, {"staging": STAGING})
dbutils.notebook.run("write_curated", 7200, {"staging": STAGING, "curated": CURATED})

print("orders_incremental_load complete")
