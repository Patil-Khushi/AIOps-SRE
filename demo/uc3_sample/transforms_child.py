# Databricks notebook source
# ---------------------------------------------------------------------------
# SAMPLE (synthetic) CHILD notebook — sales aggregation. THE RUNTIME HOG (~1440
# min of the 2160-min job). Multiple stacked anti-patterns. NOT client code.
# ---------------------------------------------------------------------------

from pyspark.sql import functions as F

staging = dbutils.widgets.get("staging")

orders = spark.read.parquet(staging)
line_items = spark.read.parquet(staging + "line_items/")

# Anti-pattern: repeated actions — each .count() recomputes `orders` from scratch
# because it is never cached.
print("orders", orders.count())
print("active", orders.filter(F.col("status") == "ACTIVE").count())
print("distinct customers", orders.select("customer_id").distinct().count())

# Anti-pattern: exploding cross-style join with no join key narrowing — the
# shuffle blows up to O(orders x line_items) before the aggregation collapses it.
joined = orders.join(line_items, orders.order_id == line_items.order_id)
agg = joined.groupBy("customer_id", "product_id").agg(F.sum("amount").alias("total"))

# Anti-pattern: pull the whole aggregation to the driver and loop in Python to
# compute a grand total, instead of a distributed .agg(F.sum(...)).
rows = agg.collect()
grand_total = 0
for r in rows:
    grand_total += r["total"]
print("grand_total", grand_total)

agg.write.mode("overwrite").parquet(staging + "sales_agg/")
