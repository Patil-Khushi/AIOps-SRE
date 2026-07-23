# Databricks notebook source
# ---------------------------------------------------------------------------
# SAMPLE (synthetic) CHILD notebook for UC3. This is the runtime hog (~1800 min
# of the 2160-min job). It carries two textbook anti-patterns the agent should
# catch. NOT client code.
# ---------------------------------------------------------------------------

staging_path = dbutils.widgets.get("staging_path")
out_path = dbutils.widgets.get("out_path")

df = spark.read.parquet(staging_path)

# Wide aggregation.
result = df.groupBy("customer_id").agg({"amount": "sum"}).withColumnRenamed("sum(amount)", "total")

# Anti-pattern #1: coalesce(1) collapses the write to a single task — the whole
# curated write serializes onto one executor. This is the dominant bottleneck.
result.coalesce(1).write.mode("overwrite").parquet(out_path)

# Anti-pattern #2: pulling the full result to the driver and looping in Python
# to compute a grand total, instead of a distributed aggregation.
rows = result.collect()
grand_total = 0
for r in rows:
    grand_total += r["total"]

print("grand_total", grand_total)
