# Databricks notebook source
# MAGIC %md
# MAGIC # Partner Detail — Health & Issues
# MAGIC
# MAGIC Materializes per-partner health scores and per-issue detail rows from raw CDC tables
# MAGIC into Genie-friendly flat tables.
# MAGIC
# MAGIC **Output:**
# MAGIC - `core_dev.techops.partner_health` — 1 row per partner: aggregate scorecard (last 90 days)
# MAGIC - `core_dev.techops.partner_issues` — 1 row per issue event: redeliveries + pipeline failures + ABF errors
# MAGIC
# MAGIC **Schedule:** Daily
# MAGIC **Source data:** contentavails_cdc (redeliveries, ABF logs), titancontrollerdb_cdc (pipeline failures)

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.types import StringType

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 1: partner_issues
# MAGIC
# MAGIC One row per issue event. Combines:
# MAGIC - Redeliveries (video/image/subtitle) — from delivery_* tables
# MAGIC - Pipeline failures (video/image/subtitle) — from Titan processing_stage
# MAGIC - ABF QA errors — from contentavails_cdc.logs
# MAGIC - Metadata upload errors — from partner_uploaded_metadata

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1a. Redeliveries (last 90 days, all partners)

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW redeliveries_raw AS
# MAGIC WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date)
# MAGIC
# MAGIC SELECT
# MAGIC   'redelivery' AS issue_type,
# MAGIC   'video' AS modality,
# MAGIC   COALESCE(ae.import_id, am.import_id) AS import_id,
# MAGIC   COALESCE(ae.content_id, am.content_id) AS content_id,
# MAGIC   CASE WHEN ae.id IS NOT NULL THEN 'episode' ELSE 'movie' END AS content_type,
# MAGIC   COALESCE(ae.title, am.title) AS title,
# MAGIC   dv.redelivery_reason AS reason,
# MAGIC   dv.redelivery_details AS details,
# MAGIC   dv.redelivery_status AS status,
# MAGIC   dv.`dismissed?` AS dismissed,
# MAGIC   dv.dismissed_reason,
# MAGIC   dv.reached_out_by AS resolved_by,
# MAGIC   dv.inserted_at AS issue_ts,
# MAGIC   GREATEST(dv.updated_at, dv.inserted_at) AS last_updated,
# MAGIC   CAST(dv.id AS STRING) AS source_id
# MAGIC FROM core_prod.contentavails_cdc.asset_videos av
# MAGIC JOIN core_prod.contentavails_cdc.delivery_videos dv ON av.id = dv.asset_id
# MAGIC LEFT JOIN core_prod.contentavails_cdc.avail_episodes ae ON av.avail_id = ae.id
# MAGIC LEFT JOIN core_prod.contentavails_cdc.avail_movies am ON av.avail_id = am.id
# MAGIC CROSS JOIN window w
# MAGIC WHERE dv.redelivery_status IS NOT NULL
# MAGIC   AND dv.inserted_at >= w.start_date
# MAGIC
# MAGIC UNION ALL
# MAGIC
# MAGIC SELECT
# MAGIC   'redelivery' AS issue_type,
# MAGIC   'image' AS modality,
# MAGIC   COALESCE(ae.import_id, am.import_id) AS import_id,
# MAGIC   COALESCE(ae.content_id, am.content_id) AS content_id,
# MAGIC   CASE WHEN ae.id IS NOT NULL THEN 'episode' ELSE 'movie' END AS content_type,
# MAGIC   COALESCE(ae.title, am.title) AS title,
# MAGIC   di.redelivery_reason AS reason,
# MAGIC   di.redelivery_details AS details,
# MAGIC   di.redelivery_status AS status,
# MAGIC   di.`dismissed?` AS dismissed,
# MAGIC   di.dismissed_reason,
# MAGIC   di.reached_out_by AS resolved_by,
# MAGIC   di.inserted_at AS issue_ts,
# MAGIC   GREATEST(di.updated_at, di.inserted_at) AS last_updated,
# MAGIC   CAST(di.id AS STRING) AS source_id
# MAGIC FROM core_prod.contentavails_cdc.asset_images ai
# MAGIC JOIN core_prod.contentavails_cdc.delivery_images di ON ai.id = di.asset_id
# MAGIC LEFT JOIN core_prod.contentavails_cdc.avail_episodes ae ON ai.avail_id = ae.id
# MAGIC LEFT JOIN core_prod.contentavails_cdc.avail_movies am ON ai.avail_id = am.id
# MAGIC CROSS JOIN window w
# MAGIC WHERE di.redelivery_status IS NOT NULL
# MAGIC   AND di.inserted_at >= w.start_date
# MAGIC
# MAGIC UNION ALL
# MAGIC
# MAGIC SELECT
# MAGIC   'redelivery' AS issue_type,
# MAGIC   'subtitle' AS modality,
# MAGIC   COALESCE(ae.import_id, am.import_id) AS import_id,
# MAGIC   COALESCE(ae.content_id, am.content_id) AS content_id,
# MAGIC   CASE WHEN ae.id IS NOT NULL THEN 'episode' ELSE 'movie' END AS content_type,
# MAGIC   COALESCE(ae.title, am.title) AS title,
# MAGIC   ds.redelivery_reason AS reason,
# MAGIC   ds.redelivery_details AS details,
# MAGIC   ds.redelivery_status AS status,
# MAGIC   ds.`dismissed?` AS dismissed,
# MAGIC   ds.dismissed_reason,
# MAGIC   ds.reached_out_by AS resolved_by,
# MAGIC   ds.inserted_at AS issue_ts,
# MAGIC   GREATEST(ds.updated_at, ds.inserted_at) AS last_updated,
# MAGIC   CAST(ds.id AS STRING) AS source_id
# MAGIC FROM core_prod.contentavails_cdc.asset_subtitles asb
# MAGIC JOIN core_prod.contentavails_cdc.delivery_subtitles ds ON asb.id = ds.asset_id
# MAGIC LEFT JOIN core_prod.contentavails_cdc.avail_episodes ae ON asb.avail_id = ae.id
# MAGIC LEFT JOIN core_prod.contentavails_cdc.avail_movies am ON asb.avail_id = am.id
# MAGIC CROSS JOIN window w
# MAGIC WHERE ds.redelivery_status IS NOT NULL
# MAGIC   AND ds.inserted_at >= w.start_date

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1b. Pipeline Failures — Titan processing (last 90 days)

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW pipeline_failures_raw AS
# MAGIC WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date),
# MAGIC
# MAGIC -- Get the most recent failed session per asset/media to avoid counting historical retries
# MAGIC latest_asset_failures AS (
# MAGIC   SELECT ps.id AS session_id, ps.asset_id, NULL AS media_id
# MAGIC   FROM core_prod.titancontrollerdb_cdc.processing_session ps
# MAGIC   JOIN (
# MAGIC     SELECT asset_id, MAX(updated_at) AS max_ts
# MAGIC     FROM core_prod.titancontrollerdb_cdc.processing_session
# MAGIC     WHERE status = 'failed' AND asset_id IS NOT NULL
# MAGIC     GROUP BY asset_id
# MAGIC   ) latest ON ps.asset_id = latest.asset_id AND ps.updated_at = latest.max_ts
# MAGIC   WHERE ps.status = 'failed'
# MAGIC ),
# MAGIC latest_media_failures AS (
# MAGIC   SELECT ps.id AS session_id, NULL AS asset_id, ps.media_id
# MAGIC   FROM core_prod.titancontrollerdb_cdc.processing_session ps
# MAGIC   JOIN (
# MAGIC     SELECT media_id, MAX(updated_at) AS max_ts
# MAGIC     FROM core_prod.titancontrollerdb_cdc.processing_session
# MAGIC     WHERE status = 'failed' AND media_id IS NOT NULL
# MAGIC     GROUP BY media_id
# MAGIC   ) latest ON ps.media_id = latest.media_id AND ps.updated_at = latest.max_ts
# MAGIC   WHERE ps.status = 'failed'
# MAGIC )
# MAGIC
# MAGIC -- Image pipeline failures
# MAGIC SELECT
# MAGIC   'pipeline_failure' AS issue_type,
# MAGIC   'image' AS modality,
# MAGIC   a.import_id,
# MAGIC   CAST(NULL AS BIGINT) AS content_id,
# MAGIC   'image' AS content_type,
# MAGIC   CAST(NULL AS STRING) AS title,
# MAGIC   st.stage AS reason,
# MAGIC   CAST(NULL AS STRING) AS details,
# MAGIC   'failed' AS status,
# MAGIC   false AS dismissed,
# MAGIC   CAST(NULL AS STRING) AS dismissed_reason,
# MAGIC   CAST(NULL AS STRING) AS resolved_by,
# MAGIC   st.updated_at AS issue_ts,
# MAGIC   st.updated_at AS last_updated,
# MAGIC   CAST(a.id AS STRING) AS source_id
# MAGIC FROM latest_asset_failures laf
# MAGIC JOIN core_prod.titancontrollerdb_cdc.asset a ON a.id = laf.asset_id
# MAGIC JOIN core_prod.titancontrollerdb_cdc.processing_stage st ON st.session_id = laf.session_id
# MAGIC CROSS JOIN window w
# MAGIC WHERE a.type = 'image'
# MAGIC   AND st.status = 'failed'
# MAGIC   AND st.updated_at >= w.start_date
# MAGIC
# MAGIC UNION ALL
# MAGIC
# MAGIC -- Video/movie pipeline failures
# MAGIC SELECT
# MAGIC   'pipeline_failure' AS issue_type,
# MAGIC   'video' AS modality,
# MAGIC   m.import_id,
# MAGIC   CAST(NULL AS BIGINT) AS content_id,
# MAGIC   'video' AS content_type,
# MAGIC   CAST(NULL AS STRING) AS title,
# MAGIC   st.stage AS reason,
# MAGIC   CAST(NULL AS STRING) AS details,
# MAGIC   'failed' AS status,
# MAGIC   false AS dismissed,
# MAGIC   CAST(NULL AS STRING) AS dismissed_reason,
# MAGIC   CAST(NULL AS STRING) AS resolved_by,
# MAGIC   st.updated_at AS issue_ts,
# MAGIC   st.updated_at AS last_updated,
# MAGIC   CAST(m.id AS STRING) AS source_id
# MAGIC FROM latest_media_failures lmf
# MAGIC JOIN core_prod.titancontrollerdb_cdc.media m ON m.id = lmf.media_id
# MAGIC JOIN core_prod.titancontrollerdb_cdc.processing_stage st ON st.session_id = lmf.session_id
# MAGIC CROSS JOIN window w
# MAGIC WHERE m.deprecated_at IS NULL
# MAGIC   AND st.status = 'failed'
# MAGIC   AND st.updated_at >= w.start_date
# MAGIC
# MAGIC UNION ALL
# MAGIC
# MAGIC -- Subtitle pipeline failures
# MAGIC SELECT
# MAGIC   'pipeline_failure' AS issue_type,
# MAGIC   'subtitle' AS modality,
# MAGIC   a.import_id,
# MAGIC   CAST(NULL AS BIGINT) AS content_id,
# MAGIC   'subtitle' AS content_type,
# MAGIC   CAST(NULL AS STRING) AS title,
# MAGIC   st.stage AS reason,
# MAGIC   CAST(NULL AS STRING) AS details,
# MAGIC   'failed' AS status,
# MAGIC   false AS dismissed,
# MAGIC   CAST(NULL AS STRING) AS dismissed_reason,
# MAGIC   CAST(NULL AS STRING) AS resolved_by,
# MAGIC   st.updated_at AS issue_ts,
# MAGIC   st.updated_at AS last_updated,
# MAGIC   CAST(a.id AS STRING) AS source_id
# MAGIC FROM latest_asset_failures laf
# MAGIC JOIN core_prod.titancontrollerdb_cdc.asset a ON a.id = laf.asset_id
# MAGIC JOIN core_prod.titancontrollerdb_cdc.processing_stage st ON st.session_id = laf.session_id
# MAGIC CROSS JOIN window w
# MAGIC WHERE a.type = 'subtitle'
# MAGIC   AND st.status = 'failed'
# MAGIC   AND st.updated_at >= w.start_date

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1c. ABF QA Errors (last 90 days)

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW abf_errors_raw AS
# MAGIC WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date)
# MAGIC
# MAGIC SELECT
# MAGIC   'abf_error' AS issue_type,
# MAGIC   CASE
# MAGIC     WHEN LOWER(l.message) LIKE '%subtitle%' THEN 'subtitle'
# MAGIC     WHEN LOWER(l.message) LIKE '%audio%' THEN 'audio'
# MAGIC     WHEN LOWER(l.message) LIKE '%color bar%' THEN 'video'
# MAGIC     WHEN LOWER(l.message) RLIKE 'count ?down' THEN 'video'
# MAGIC     WHEN LOWER(l.message) LIKE '%black screen%' THEN 'video'
# MAGIC     ELSE 'other'
# MAGIC   END AS modality,
# MAGIC   a.import_id,
# MAGIC   a.content_id,
# MAGIC   a.content_type,
# MAGIC   ci.title,
# MAGIC   CASE
# MAGIC     WHEN LOWER(l.message) LIKE '%subtitle%' THEN 'subtitle_issue'
# MAGIC     WHEN LOWER(l.message) LIKE '%audio%' THEN 'audio_issue'
# MAGIC     WHEN LOWER(l.message) LIKE '%color bar%' THEN 'color_bar'
# MAGIC     WHEN LOWER(l.message) RLIKE 'count ?down' THEN 'countdown'
# MAGIC     WHEN LOWER(l.message) LIKE '%black screen%' THEN 'black_screen'
# MAGIC     ELSE 'other_qa_issue'
# MAGIC   END AS reason,
# MAGIC   l.message AS details,
# MAGIC   'flagged' AS status,
# MAGIC   false AS dismissed,
# MAGIC   CAST(NULL AS STRING) AS dismissed_reason,
# MAGIC   CAST(NULL AS STRING) AS resolved_by,
# MAGIC   l.inserted_at AS issue_ts,
# MAGIC   l.inserted_at AS last_updated,
# MAGIC   CAST(l.assessment_id AS STRING) AS source_id
# MAGIC FROM core_prod.contentavails_cdc.logs l
# MAGIC JOIN core_prod.contentavails_cdc.assessments a ON l.assessment_id = a.id
# MAGIC LEFT JOIN core_prod.tubidw.content_info ci ON ci.content_id = a.content_id
# MAGIC CROSS JOIN window w
# MAGIC WHERE l.message IS NOT NULL
# MAGIC   AND l.inserted_at >= w.start_date
# MAGIC   AND LOWER(l.message) RLIKE 'subtitle|color bars?|count ?down|black screen|audio'

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1d. Metadata Upload Errors (last 90 days)

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW metadata_errors_raw AS
# MAGIC WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date)
# MAGIC
# MAGIC SELECT
# MAGIC   'metadata_error' AS issue_type,
# MAGIC   'metadata' AS modality,
# MAGIC   import_id,
# MAGIC   CAST(NULL AS BIGINT) AS content_id,
# MAGIC   'metadata' AS content_type,
# MAGIC   CAST(NULL AS STRING) AS title,
# MAGIC   COALESCE(error_type, 'metadata_error') AS reason,
# MAGIC   CAST(errors AS STRING) AS details,
# MAGIC   'failed' AS status,
# MAGIC   false AS dismissed,
# MAGIC   CAST(NULL AS STRING) AS dismissed_reason,
# MAGIC   CAST(NULL AS STRING) AS resolved_by,
# MAGIC   COALESCE(updated_at, inserted_at) AS issue_ts,
# MAGIC   COALESCE(updated_at, inserted_at) AS last_updated,
# MAGIC   CAST(id AS STRING) AS source_id
# MAGIC FROM core_prod.contentavails_cdc.partner_uploaded_metadata
# MAGIC CROSS JOIN window w
# MAGIC WHERE COALESCE(updated_at, inserted_at) >= w.start_date
# MAGIC   AND REGEXP_REPLACE(TRANSLATE(TRIM(CAST(errors AS STRING)), '[]{}"', ''), '\\s+', '') <> ''

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1e. Union all issues and write to Delta

# COMMAND ----------

from pyspark.sql import functions as F

redeliveries = spark.table("redeliveries_raw")
pipeline = spark.table("pipeline_failures_raw")
abf = spark.table("abf_errors_raw")
metadata = spark.table("metadata_errors_raw")

partner_issues = (
    redeliveries
    .unionByName(pipeline)
    .unionByName(abf)
    .unionByName(metadata)
    .withColumn("snapshot_date", F.current_date())
    .filter(F.col("import_id").isNotNull())
    .select(
        "snapshot_date",
        "issue_type",
        "modality",
        "import_id",
        "content_id",
        "content_type",
        "title",
        "reason",
        "details",
        "status",
        "dismissed",
        "dismissed_reason",
        "resolved_by",
        "issue_ts",
        "last_updated",
        "source_id",
    )
)

print(f"Total issue rows: {partner_issues.count()}")
print(f"By issue_type:")
partner_issues.groupBy("issue_type").count().show()

# COMMAND ----------

OUTPUT_ISSUES = "core_dev.techops.partner_issues"

partner_issues.write.format("delta").mode("overwrite").option(
    "overwriteSchema", "true"
).saveAsTable(OUTPUT_ISSUES)

print(f"Written to {OUTPUT_ISSUES}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 2: partner_health
# MAGIC
# MAGIC One row per partner with aggregate health metrics over the last 90 days.
# MAGIC Combines issue counts from partner_issues with file volume from Titan.

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW file_counts_by_partner AS
# MAGIC WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date)
# MAGIC
# MAGIC SELECT
# MAGIC   import_id,
# MAGIC   COUNT(DISTINCT CASE WHEN type = 'image' THEN id END) AS total_image_files,
# MAGIC   COUNT(DISTINCT CASE WHEN type = 'subtitle' THEN id END) AS total_subtitle_files
# MAGIC FROM core_prod.titancontrollerdb_cdc.asset
# MAGIC CROSS JOIN window w
# MAGIC WHERE COALESCE(updated_at, inserted_at) >= w.start_date
# MAGIC   AND type IN ('image', 'subtitle')
# MAGIC   AND import_id IS NOT NULL
# MAGIC GROUP BY import_id
# MAGIC
# MAGIC UNION ALL
# MAGIC
# MAGIC -- Video files come from the media table
# MAGIC SELECT
# MAGIC   import_id,
# MAGIC   0 AS total_image_files,
# MAGIC   0 AS total_subtitle_files
# MAGIC FROM core_prod.titancontrollerdb_cdc.media
# MAGIC CROSS JOIN window w
# MAGIC WHERE COALESCE(updated_at, inserted_at) >= w.start_date
# MAGIC   AND import_id IS NOT NULL
# MAGIC GROUP BY import_id

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW video_file_counts AS
# MAGIC WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date)
# MAGIC SELECT
# MAGIC   import_id,
# MAGIC   COUNT(DISTINCT id) AS total_video_files
# MAGIC FROM core_prod.titancontrollerdb_cdc.media
# MAGIC CROSS JOIN window w
# MAGIC WHERE COALESCE(updated_at, inserted_at) >= w.start_date
# MAGIC   AND import_id IS NOT NULL
# MAGIC GROUP BY import_id

# COMMAND ----------

issues_df = spark.table(OUTPUT_ISSUES)

issue_agg = (
    issues_df
    .groupBy("import_id")
    .agg(
        # Total issues
        F.count("*").alias("total_issues_90d"),

        # By issue type
        F.sum(F.when(F.col("issue_type") == "redelivery", 1).otherwise(0)).alias("redelivery_count"),
        F.sum(F.when(F.col("issue_type") == "pipeline_failure", 1).otherwise(0)).alias("pipeline_failure_count"),
        F.sum(F.when(F.col("issue_type") == "abf_error", 1).otherwise(0)).alias("abf_error_count"),
        F.sum(F.when(F.col("issue_type") == "metadata_error", 1).otherwise(0)).alias("metadata_error_count"),

        # Redeliveries by modality
        F.sum(F.when((F.col("issue_type") == "redelivery") & (F.col("modality") == "video"), 1).otherwise(0)).alias("redeliv_video"),
        F.sum(F.when((F.col("issue_type") == "redelivery") & (F.col("modality") == "image"), 1).otherwise(0)).alias("redeliv_image"),
        F.sum(F.when((F.col("issue_type") == "redelivery") & (F.col("modality") == "subtitle"), 1).otherwise(0)).alias("redeliv_subtitle"),

        # Pipeline failures by modality
        F.sum(F.when((F.col("issue_type") == "pipeline_failure") & (F.col("modality") == "video"), 1).otherwise(0)).alias("pipeline_fail_video"),
        F.sum(F.when((F.col("issue_type") == "pipeline_failure") & (F.col("modality") == "image"), 1).otherwise(0)).alias("pipeline_fail_image"),
        F.sum(F.when((F.col("issue_type") == "pipeline_failure") & (F.col("modality") == "subtitle"), 1).otherwise(0)).alias("pipeline_fail_subtitle"),

        # ABF error categories
        F.sum(F.when(F.col("reason") == "subtitle_issue", 1).otherwise(0)).alias("abf_subtitle_issues"),
        F.sum(F.when(F.col("reason") == "audio_issue", 1).otherwise(0)).alias("abf_audio_issues"),
        F.sum(F.when(F.col("reason") == "color_bar", 1).otherwise(0)).alias("abf_color_bar_issues"),
        F.sum(F.when(F.col("reason") == "countdown", 1).otherwise(0)).alias("abf_countdown_issues"),
        F.sum(F.when(F.col("reason") == "black_screen", 1).otherwise(0)).alias("abf_black_screen_issues"),

        # Dismissed redeliveries
        F.sum(F.when((F.col("issue_type") == "redelivery") & (F.col("dismissed") == True), 1).otherwise(0)).alias("redeliveries_dismissed"),

        # Distinct titles affected
        F.countDistinct(F.when(F.col("content_id").isNotNull(), F.col("content_id"))).alias("distinct_titles_affected"),

        # Most recent issue
        F.max("issue_ts").alias("most_recent_issue_ts"),

        # Top reason
        F.first(
            F.when(F.col("issue_type") == "redelivery", F.col("reason")),
            ignorenulls=True
        ).alias("top_redelivery_reason"),
    )
)

# COMMAND ----------

# Join with file counts to compute error rate
file_counts = spark.sql("""
    WITH window AS (SELECT DATE_SUB(CURRENT_DATE, 90) AS start_date)
    SELECT
        COALESCE(a.import_id, m.import_id) AS import_id,
        COALESCE(img_count, 0) AS total_image_files,
        COALESCE(sub_count, 0) AS total_subtitle_files,
        COALESCE(vid_count, 0) AS total_video_files
    FROM (
        SELECT import_id,
               COUNT(DISTINCT CASE WHEN type = 'image' THEN id END) AS img_count,
               COUNT(DISTINCT CASE WHEN type = 'subtitle' THEN id END) AS sub_count
        FROM core_prod.titancontrollerdb_cdc.asset
        CROSS JOIN window w
        WHERE COALESCE(updated_at, inserted_at) >= w.start_date
          AND type IN ('image', 'subtitle')
          AND import_id IS NOT NULL
        GROUP BY import_id
    ) a
    FULL OUTER JOIN (
        SELECT import_id, COUNT(DISTINCT id) AS vid_count
        FROM core_prod.titancontrollerdb_cdc.media
        CROSS JOIN window w
        WHERE COALESCE(updated_at, inserted_at) >= w.start_date
          AND import_id IS NOT NULL
        GROUP BY import_id
    ) m ON LOWER(a.import_id) = LOWER(m.import_id)
""")

# Join with partner_summary for name and metadata
partner_info = (
    spark.table("core_dev.techops.partner_summary")
    .filter(F.col("snapshot_date") == F.expr("(SELECT MAX(snapshot_date) FROM core_dev.techops.partner_summary)"))
    .select("import_id", "name", "partner_status", "is_priority_partner", "type", "pops_poc", "cops_poc")
)

partner_health = (
    issue_agg
    .join(file_counts, on="import_id", how="left")
    .join(partner_info, on="import_id", how="left")
    .withColumn("total_files_90d",
        F.col("total_video_files") + F.col("total_image_files") + F.col("total_subtitle_files")
    )
    .withColumn("error_rate_pct",
        F.when(F.col("total_files_90d") > 0,
            F.round(F.col("total_issues_90d") * 100.0 / F.col("total_files_90d"), 2)
        ).otherwise(F.lit(None))
    )
    .withColumn("snapshot_date", F.current_date())
    .select(
        "snapshot_date",
        "import_id",
        "name",
        "partner_status",
        "is_priority_partner",
        "type",
        "pops_poc",
        "cops_poc",
        "total_files_90d",
        "total_video_files",
        "total_image_files",
        "total_subtitle_files",
        "total_issues_90d",
        "error_rate_pct",
        "redelivery_count",
        "pipeline_failure_count",
        "abf_error_count",
        "metadata_error_count",
        "redeliv_video",
        "redeliv_image",
        "redeliv_subtitle",
        "pipeline_fail_video",
        "pipeline_fail_image",
        "pipeline_fail_subtitle",
        "abf_subtitle_issues",
        "abf_audio_issues",
        "abf_color_bar_issues",
        "abf_countdown_issues",
        "abf_black_screen_issues",
        "redeliveries_dismissed",
        "distinct_titles_affected",
        "most_recent_issue_ts",
        "top_redelivery_reason",
    )
)

print(f"Partner health rows: {partner_health.count()}")
partner_health.orderBy(F.desc("total_issues_90d")).show(10, truncate=False)

# COMMAND ----------

OUTPUT_HEALTH = "core_dev.techops.partner_health"

partner_health.write.format("delta").mode("overwrite").option(
    "overwriteSchema", "true"
).saveAsTable(OUTPUT_HEALTH)

print(f"Written to {OUTPUT_HEALTH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation

# COMMAND ----------

print("=== partner_issues ===")
display(
    spark.table(OUTPUT_ISSUES)
    .groupBy("issue_type", "modality")
    .count()
    .orderBy("issue_type", "modality")
)

# COMMAND ----------

print("=== partner_health — top 10 by error rate ===")
display(
    spark.table(OUTPUT_HEALTH)
    .filter(F.col("total_files_90d") > 100)
    .orderBy(F.desc("error_rate_pct"))
    .limit(10)
    .select("import_id", "name", "total_files_90d", "total_issues_90d", "error_rate_pct",
            "redelivery_count", "pipeline_failure_count", "abf_error_count")
)

# COMMAND ----------

print("=== partner_health — priority partners ===")
display(
    spark.table(OUTPUT_HEALTH)
    .filter(F.col("is_priority_partner") == True)
    .orderBy(F.desc("total_issues_90d"))
    .select("import_id", "name", "total_issues_90d", "error_rate_pct",
            "redelivery_count", "pipeline_failure_count", "abf_error_count")
)
