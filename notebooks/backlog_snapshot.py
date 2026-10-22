# Databricks notebook source
# MAGIC %md
# MAGIC # Backlog Snapshot — copy of DSA's CDP Backlog Data Model
# MAGIC
# MAGIC Materializes a straight copy of DSA's per-title backlog snapshot into `core_dev.techops`
# MAGIC so the Argo "Backlog" Genie space reads the same source of truth as DSA without needing
# MAGIC its own grants on `core_dev.dsa`. No re-derivation: when DSA's model changes (e.g. V4.2
# MAGIC promotion), only `SOURCE_TABLE` below changes.
# MAGIC
# MAGIC **Output:** `core_dev.techops.backlog_snapshot` — one row per content_id per snapshot date (`ds`)
# MAGIC **Source:** `core_dev.dsa.dsac_cdp_backlog_snapshot` (CDP Backlog Data Model)
# MAGIC **Schedule:** Daily, after DSA's snapshot lands (add to the "Content Metric Snapshots" job)

# COMMAND ----------

SOURCE_TABLE = "core_dev.dsa.dsac_cdp_backlog_snapshot"
TARGET_TABLE = "core_dev.techops.backlog_snapshot"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Copy the source table

# COMMAND ----------

spark.sql(f"CREATE OR REPLACE TABLE {TARGET_TABLE} AS SELECT * FROM {SOURCE_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Validate the copy matches the source

# COMMAND ----------

src = spark.sql(f"SELECT COUNT(*) c, COUNT(DISTINCT ds) d, MAX(ds) m FROM {SOURCE_TABLE}").first()
tgt = spark.sql(f"SELECT COUNT(*) c, COUNT(DISTINCT ds) d, MAX(ds) m FROM {TARGET_TABLE}").first()
print(f"source: {src.c} rows, {src.d} snapshots, latest {src.m}")
print(f"target: {tgt.c} rows, {tgt.d} snapshots, latest {tgt.m}")
assert (src.c, src.d, src.m) == (tgt.c, tgt.d, tgt.m), "Copy does not match source"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Add column comments for Genie discoverability
# MAGIC
# MAGIC Only columns whose meaning is clear from the model are commented. Bucket codes (A1..P3, D0)
# MAGIC are not in this table — they are assigned by a separate dashboard build script.

# COMMAND ----------

comments = {
    "ds": "Snapshot date. ALWAYS filter to the latest snapshot: ds = (SELECT MAX(ds) FROM core_dev.techops.backlog_snapshot) unless the question asks for a trend or a past date.",
    "content_id": "Tubi content ID of the title (movie, series or episode row). One row per content_id per ds.",
    "content_type": "Type of row: episode or movie (there are no series-level rows). Most rows are episodes. To count series or programs once, filter is_program_representative = true or count DISTINCT program_id.",
    "program_id": "Program ID. Titles in the same program share it; see is_program_representative to count a program once.",
    "import_id": "Partner/provider import ID the title was delivered under.",
    "partner_name": "Display name of the content partner.",
    "title_priority": "Priority level of the title (0 to 4) as set by content ops. 1 is the highest tier in volume terms; confirm the meaning of each level with content ops before describing it.",
    "is_priority_partner": "True if the title belongs to a priority partner.",
    "is_creator": "True for creator content. Creator titles follow a different publish cadence and are usually reported separately.",
    "pipeline_stage": "Current stage in the content pipeline: ABF Incomplete, ABF Incomplete (regressed), ABF Complete, In Refresh, Ready to Publish, Published.",
    "assessment_status": "ABF assessment status: DONE, REDELIVERY, DEACTIVATED, INTERNAL_REVIEW, PENDING_REVIEW, REVIEWING, BACKLOG. ABF backlog = PENDING_REVIEW + INTERNAL_REVIEW.",
    "abf_completion_type": "How the ABF assessment was completed.",
    "assessment_cycle_count": "Number of ABF assessment cycles the title has been through.",
    "redelivery_count": "Number of redeliveries recorded for the title.",
    "metadata_status": "Metadata review tool status: COMPLETE, INIT, PENDING, DELETED, or NULL if not in the tool. DELETED means the metadata team removed it from the workflow; treat like COMPLETE.",
    "metadata_review_status": "Metadata review status.",
    "refresh_status": "Content Refresh status: TITLE_STATUS_NOT_STARTED, TITLE_STATUS_IN_PROGRESS, TITLE_STATUS_BLOCKED, TITLE_STATUS_READY_TO_PUBLISH, TITLE_STATUS_PUBLISHED, TITLE_STATUS_DEACTIVATED, or NULL if the title is not in Refresh.",
    "policy_review_status": "Status of the policy review.",
    "image_review_status": "Status of the image review.",
    "episodes_review_status": "Status of the episodes review (series).",
    "curation_review_status": "Status of the curation review.",
    "avail_status": "Status of the avail record.",
    "days_in_pipeline": "Days since the title entered the pipeline.",
    "days_in_current_stage": "Days the title has been in its current pipeline_stage.",
    "deal_type": "US deal type: RevShare, CASH or RECOUP_ON_PUB.",
    "total_fee": "US total deal license fee for Cash deals, or MG fee for MG and MGPool deals.",
    "deal_start": "US deal start date.",
    "deal_end": "US deal end date.",
    "is_deal_active": "1 if the US deal is active on the snapshot date, else 0 (only about 1% of titles are 1).",
    "window_start": "Start date of the title's US policy window.",
    "window_end": "End date of the title's US policy window.",
    "window_count": "Number of US policy windows for the title.",
    "window_status": "US policy window status: In Window, Not Started, Expired, No Window.",
    "blocking_gates": "Gates currently blocking the title from going live.",
    "non_us_deal_type": "Deal type for non-US committed deals.",
    "non_us_total_fee": "Total fee for non-US committed deals.",
    "non_us_deal_territories": "Territories covered by the non-US deal.",
    "has_committed_deal": "True if the title has a committed deal in any territory.",
    "committed_deal_scope": "Where the committed deal applies: us, non_us, both or none.",
    "deal_type_any_territory": "Deal type considering all territories.",
    "total_committed_fee_all_territories": "Total committed fee across US and non-US territories. Use with is_program_representative = true to avoid double counting fees across a program.",
    "is_program_representative": "True for exactly one title per program. Filter on this to count a program or sum fees once.",
    "non_us_window_start": "Start date of the non-US policy window.",
    "non_us_window_end": "End date of the non-US policy window.",
    "non_us_window_status": "Non-US policy window status.",
    "is_launch_failed": "True if the title's last launch attempt failed.",
    "last_launch_failed_at": "Timestamp of the most recent launch failure.",
    "launch_failure_count": "Number of launch failures for the title.",
    "launch_failure_reason": "Reason for the most recent launch failure.",
    "launch_attempt_count": "Number of launch attempts.",
    "launch_never_attempted": "True if no launch has ever been attempted for the title.",
    "has_open_redelivery": "True if the title has at least one open redelivery.",
    "has_no_active_delivery": "True if the title has no active delivery.",
    "has_dismissed_redelivery": "True if a redelivery for the title was dismissed.",
    "ready_to_publish_since": "Timestamp the title became ready to publish.",
    "days_ready_to_publish": "Days the title has been ready to publish.",
}

existing = {f.name for f in spark.table(TARGET_TABLE).schema.fields}
for col, comment in comments.items():
    if col in existing:
        escaped = comment.replace("'", "\\'")
        spark.sql(f"ALTER TABLE {TARGET_TABLE} ALTER COLUMN {col} COMMENT '{escaped}'")
print(f"Commented {len([c for c in comments if c in existing])} of {len(existing)} columns")
print("Missing from table:", [c for c in comments if c not in existing])
