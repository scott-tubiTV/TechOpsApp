# Databricks notebook source
# MAGIC %md
# MAGIC # Content Library — Denormalized Title View
# MAGIC
# MAGIC Materializes a flat, per-title table joining CDC source tables into a Genie-friendly
# MAGIC format for title-level queries (internal tags, live status, partner, metadata).
# MAGIC
# MAGIC **Output:**
# MAGIC - `core_dev.techops.content_library` — 1 row per content_id: title metadata + live status + internal tags
# MAGIC
# MAGIC **Schedule:** Daily
# MAGIC **Source data:** core_prod.contentdb_cdc (titles, delivery, windows), core_dev.techops (partner_title_status, content_first_live, v_dupe_checker_titles)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Build base from delivery + titles (content_id, title metadata, internal tags)

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW base_titles AS
# MAGIC SELECT * FROM (
# MAGIC   SELECT
# MAGIC     d.content_id,
# MAGIC     d.title_id,
# MAGIC     d.import_id,
# MAGIC     d.assessment_status,
# MAGIC     d.active AS delivery_active,
# MAGIC     t.release_year,
# MAGIC     t.genres,
# MAGIC     t.imdb_id,
# MAGIC     t.tubi_fields,
# MAGIC     CASE
# MAGIC       WHEN t.tubi_fields LIKE '%creator_content%' THEN true
# MAGIC       ELSE false
# MAGIC     END AS is_creator_content,
# MAGIC     CASE
# MAGIC       WHEN t.tubi_fields LIKE '%same_day_content%' THEN true
# MAGIC       ELSE false
# MAGIC     END AS is_same_day_content,
# MAGIC     CASE
# MAGIC       WHEN t.tubi_fields LIKE '%tubi_original%' OR t.tubi_fields LIKE '%TUBI_ORIGINAL%' THEN true
# MAGIC       ELSE false
# MAGIC     END AS is_tubi_original,
# MAGIC     CASE
# MAGIC       WHEN t.tubi_fields LIKE '%self_service_portal%' THEN true
# MAGIC       ELSE false
# MAGIC     END AS is_self_service_portal,
# MAGIC     CASE
# MAGIC       WHEN t.tubi_fields LIKE '%world_cup%' THEN true
# MAGIC       ELSE false
# MAGIC     END AS is_world_cup,
# MAGIC     CASE
# MAGIC       WHEN t.tubi_fields LIKE '%pre_programming%' OR t.tubi_fields LIKE '%PRE_PROGRAMMING%' THEN true
# MAGIC       ELSE false
# MAGIC     END AS is_pre_programming,
# MAGIC     ROW_NUMBER() OVER (PARTITION BY d.content_id ORDER BY d.active DESC, d.updated_at DESC) AS rn
# MAGIC   FROM core_prod.contentdb_cdc.delivery d
# MAGIC   JOIN core_prod.contentdb_cdc.titles t ON d.title_id = t.id
# MAGIC   WHERE d.content_id IS NOT NULL
# MAGIC     AND t.deleted_at IS NULL
# MAGIC ) WHERE rn = 1

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Determine live status from policy windows

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW live_windows AS
# MAGIC SELECT
# MAGIC   d.content_id,
# MAGIC   w.enabled,
# MAGIC   w.start_at AS policy_start,
# MAGIC   w.end_at AS policy_end,
# MAGIC   w.included_countries,
# MAGIC   w.excluded_countries,
# MAGIC   CASE
# MAGIC     WHEN w.enabled = 'true'
# MAGIC       AND w.start_at <= CURRENT_TIMESTAMP()
# MAGIC       AND w.end_at >= CURRENT_TIMESTAMP()
# MAGIC     THEN true
# MAGIC     ELSE false
# MAGIC   END AS is_currently_live,
# MAGIC   ROW_NUMBER() OVER (
# MAGIC     PARTITION BY d.content_id
# MAGIC     ORDER BY w.start_at DESC
# MAGIC   ) AS rn
# MAGIC FROM core_prod.contentdb_cdc.windows w
# MAGIC JOIN core_prod.contentdb_cdc.delivery d ON w.delivery_id = d.id
# MAGIC WHERE d.content_id IS NOT NULL

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Get title names and content type from dupe checker view

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW title_names AS
# MAGIC SELECT
# MAGIC   content_id,
# MAGIC   source_title AS title,
# MAGIC   content_type,
# MAGIC   avail_status,
# MAGIC   active AS title_active,
# MAGIC   ROW_NUMBER() OVER (
# MAGIC     PARTITION BY content_id
# MAGIC     ORDER BY
# MAGIC       CASE WHEN active = true THEN 0 ELSE 1 END,
# MAGIC       CASE WHEN avail_status = 'imported' THEN 0 ELSE 1 END
# MAGIC   ) AS rn
# MAGIC FROM core_dev.techops.v_dupe_checker_titles
# MAGIC WHERE content_id IS NOT NULL

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Get asset status from partner_title_status (latest snapshot)

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMP VIEW asset_status AS
# MAGIC SELECT
# MAGIC   content_id,
# MAGIC   avail_status,
# MAGIC   asset_status,
# MAGIC   has_video,
# MAGIC   has_images,
# MAGIC   has_subtitles,
# MAGIC   open_redeliveries,
# MAGIC   ROW_NUMBER() OVER (
# MAGIC     PARTITION BY content_id
# MAGIC     ORDER BY updated_at DESC
# MAGIC   ) AS rn
# MAGIC FROM core_dev.techops.partner_title_status
# MAGIC WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.partner_title_status)
# MAGIC   AND content_id IS NOT NULL

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Join everything and write to content_library

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TABLE core_dev.techops.content_library AS
# MAGIC
# MAGIC WITH joined AS (
# MAGIC   SELECT
# MAGIC     bt.content_id,
# MAGIC     bt.title_id,
# MAGIC     tn.title,
# MAGIC     tn.content_type,
# MAGIC     bt.import_id AS partner,
# MAGIC     bt.release_year,
# MAGIC     bt.genres,
# MAGIC     bt.imdb_id,
# MAGIC     bt.is_creator_content,
# MAGIC     bt.is_same_day_content,
# MAGIC     bt.is_tubi_original,
# MAGIC     bt.is_self_service_portal,
# MAGIC     bt.is_world_cup,
# MAGIC     bt.is_pre_programming,
# MAGIC     bt.tubi_fields AS tubi_fields_raw,
# MAGIC     COALESCE(lw.is_currently_live, false) AS is_live,
# MAGIC     lw.policy_start,
# MAGIC     lw.policy_end,
# MAGIC     lw.included_countries,
# MAGIC     bt.assessment_status,
# MAGIC     COALESCE(ast.avail_status, tn.avail_status) AS avail_status,
# MAGIC     ast.asset_status,
# MAGIC     COALESCE(ast.has_video, false) AS has_video,
# MAGIC     COALESCE(ast.has_images, false) AS has_images,
# MAGIC     COALESCE(ast.has_subtitles, false) AS has_subtitles,
# MAGIC     COALESCE(ast.open_redeliveries, 0) AS open_redeliveries,
# MAGIC     bt.delivery_active,
# MAGIC     COALESCE(tn.title_active, false) AS title_active,
# MAGIC     cfl.first_time_set_live AS first_live_date,
# MAGIC     CURRENT_DATE() AS snapshot_date,
# MAGIC     ROW_NUMBER() OVER (PARTITION BY bt.content_id ORDER BY bt.delivery_active DESC) AS final_rn
# MAGIC   FROM base_titles bt
# MAGIC   LEFT JOIN live_windows lw
# MAGIC     ON bt.content_id = lw.content_id AND lw.rn = 1
# MAGIC   LEFT JOIN title_names tn
# MAGIC     ON bt.content_id = tn.content_id AND tn.rn = 1
# MAGIC   LEFT JOIN asset_status ast
# MAGIC     ON bt.content_id = ast.content_id AND ast.rn = 1
# MAGIC   LEFT JOIN core_dev.techops.content_first_live cfl
# MAGIC     ON bt.content_id = cfl.content_id
# MAGIC )
# MAGIC SELECT * EXCEPT (final_rn) FROM joined WHERE final_rn = 1

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Add column comments for Genie discoverability

# COMMAND ----------

comments = {
    "content_id": "Unique content identifier (join key to all other systems)",
    "title": "Title name (movie, series, or episode name)",
    "content_type": "MOVIE, SERIES, or EPISODE",
    "partner": "Partner import_id (e.g. lionsgate, sony-pictures, kinonation). Use for partner filtering.",
    "release_year": "Original release year of the title",
    "genres": "Comma-separated genre tags",
    "imdb_id": "IMDB identifier (tt-prefixed)",
    "is_creator_content": "True if title has the creator_content internal tag (creator/self-service ingestion)",
    "is_same_day_content": "True if title has the same_day_content internal tag (expedited processing)",
    "is_tubi_original": "True if title has the TUBI_ORIGINAL internal tag",
    "is_self_service_portal": "True if title was submitted via the self-service portal",
    "is_world_cup": "True if title has the world_cup internal tag",
    "is_pre_programming": "True if title is pre-programming (not yet scheduled)",
    "tubi_fields_raw": "Raw JSON of all tubi_fields from contentdb. Parse for tags not broken out as columns.",
    "is_live": "True if title has an enabled policy window that is currently active (start <= today <= end)",
    "policy_start": "Start date of the most recent policy window",
    "policy_end": "End date of the most recent policy window",
    "included_countries": "Countries where title is available (from policy window)",
    "assessment_status": "QA/ABF assessment status: DONE, PENDING, IN_PROGRESS, etc.",
    "avail_status": "Avail status: imported, pending, cancelled, etc.",
    "asset_status": "Asset delivery status: Fully Received, Min Received, Not Received, Partially Received",
    "has_video": "True if video asset has been delivered for this title",
    "has_images": "True if image assets have been delivered for this title",
    "has_subtitles": "True if subtitle assets have been delivered for this title",
    "open_redeliveries": "Number of currently open redelivery requests for this title",
    "delivery_active": "Whether the delivery record is active in contentdb",
    "title_active": "Whether the title is active in the content system",
    "first_live_date": "Date the title first went live on Tubi (earliest policy activation)",
    "snapshot_date": "Date this row was last refreshed",
}

for col, comment in comments.items():
    escaped = comment.replace("'", "\\'")
    spark.sql(f"ALTER TABLE core_dev.techops.content_library ALTER COLUMN {col} COMMENT '{escaped}'")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7: Verify output

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT
# MAGIC   COUNT(*) AS total_titles,
# MAGIC   COUNT(CASE WHEN is_live = true THEN 1 END) AS live_titles,
# MAGIC   COUNT(CASE WHEN is_creator_content = true THEN 1 END) AS creator_content,
# MAGIC   COUNT(CASE WHEN is_same_day_content = true THEN 1 END) AS same_day_content,
# MAGIC   COUNT(CASE WHEN is_tubi_original = true THEN 1 END) AS tubi_originals,
# MAGIC   COUNT(CASE WHEN is_live = true AND is_creator_content = true THEN 1 END) AS live_creator
# MAGIC FROM core_dev.techops.content_library

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Sample: Moses's original question — live creator_content titles
# MAGIC SELECT content_id, title, partner, content_type, is_live, policy_start, policy_end
# MAGIC FROM core_dev.techops.content_library
# MAGIC WHERE is_creator_content = true AND is_live = true
# MAGIC ORDER BY title
# MAGIC LIMIT 20
