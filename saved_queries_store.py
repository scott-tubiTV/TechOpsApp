"""Persistence layer for saved queries / reports in Argo."""

import json
import time
import uuid
from datetime import datetime, timezone

import streamlit as st
from databricks.sdk import WorkspaceClient

TABLE = "core_dev.techops.argo_saved_queries"
WAREHOUSE_ID = "a6b9541289d75c6e"
MAX_CACHED_ROWS = 100


REPORT_DEFAULTS = [
    {
        "title": "ABF Backlog",
        "description": "Titles waiting for ABF review (PENDING_REVIEW + INTERNAL_REVIEW)",
        "sql_text": "SELECT SUM(count) as backlog_titles FROM core_dev.techops.qa_abf_summary WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.qa_abf_summary) AND metric_type = 'current_queue' AND status IN ('PENDING_REVIEW', 'INTERNAL_REVIEW') AND team != 'System'",
        "display_type": "metric",
        "category": "ABF",
    },
    {
        "title": "Imports This Month",
        "description": "Total titles imported in the current month",
        "sql_text": "SELECT SUM(title_count) as imports FROM core_dev.techops.imported_titles_monthly WHERE month = date_trunc('month', current_date())",
        "display_type": "metric",
        "category": "Imports",
    },
    {
        "title": "Open Redeliveries",
        "description": "Total open redeliveries across all partners",
        "sql_text": "SELECT SUM(count) as open_redeliveries FROM core_dev.techops.redelivery_weekly WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.redelivery_weekly) AND status = 'open'",
        "display_type": "metric",
        "category": "Redeliveries",
    },
    {
        "title": "Reviews by Team (MTD)",
        "description": "ABF reviews completed this month by team",
        "sql_text": "SELECT team, SUM(count) as titles_reviewed FROM core_dev.techops.qa_abf_summary WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.qa_abf_summary) AND metric_type = 'monthly_review' AND status = 'DONE' AND month = date_trunc('month', current_date()) AND team IN ('Ops', 'Contractor', 'System') GROUP BY team ORDER BY titles_reviewed DESC",
        "display_type": "bar_chart",
        "category": "ABF",
    },
    {
        "title": "Redeliveries by Modality",
        "description": "Open redeliveries broken down by type (video/image/subtitle)",
        "sql_text": "SELECT modality, SUM(count) as total FROM core_dev.techops.redelivery_weekly WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.redelivery_weekly) AND status = 'open' GROUP BY modality ORDER BY total DESC",
        "display_type": "pie_chart",
        "category": "Redeliveries",
    },
    {
        "title": "Weekly Import Trend",
        "description": "Imports per week for the last 8 weeks",
        "sql_text": "SELECT date_trunc('week', month) as week, SUM(title_count) as imports FROM core_dev.techops.imported_titles_monthly WHERE month >= current_date() - INTERVAL 56 DAY GROUP BY 1 ORDER BY 1",
        "display_type": "line_chart",
        "category": "Imports",
    },
    {
        "title": "ABF Queue by Team",
        "description": "Current PENDING_REVIEW queue size by team",
        "sql_text": "SELECT team, SUM(count) as queue_size FROM core_dev.techops.qa_abf_summary WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.qa_abf_summary) AND metric_type = 'current_queue' AND status = 'PENDING_REVIEW' AND team != 'System' GROUP BY team ORDER BY queue_size DESC",
        "display_type": "bar_chart",
        "category": "ABF",
    },
    {
        "title": "Titles Expiring This Month",
        "description": "Titles with policy windows ending this month",
        "sql_text": "SELECT COUNT(*) as expiring FROM core_dev.techops.policy_window_snapshots WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.policy_window_snapshots) AND policy_end >= date_trunc('month', current_date()) AND policy_end < date_trunc('month', current_date()) + INTERVAL 1 MONTH AND disabled = false",
        "display_type": "metric",
        "category": "Policy",
    },
]


@st.cache_resource
def _get_workspace_client():
    return WorkspaceClient()


def _execute_sql(query):
    w = _get_workspace_client()
    result = w.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body={
            "statement": query,
            "warehouse_id": WAREHOUSE_ID,
            "wait_timeout": "50s",
        },
    )
    status = result.get("status", {}).get("state")
    if status in ("PENDING", "RUNNING"):
        stmt_id = result.get("statement_id")
        for _ in range(30):
            time.sleep(2)
            result = w.api_client.do("GET", f"/api/2.0/sql/statements/{stmt_id}")
            status = result.get("status", {}).get("state")
            if status not in ("PENDING", "RUNNING"):
                break
    if status != "SUCCEEDED":
        error = result.get("status", {}).get("error", {}).get("message", "Unknown")
        raise RuntimeError(f"Query failed ({status}): {error}")
    columns = [c["name"] for c in result.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = result.get("result", {}).get("data_array", [])
    return columns, rows


def _execute_sql_dicts(query):
    columns, rows = _execute_sql(query)
    return [dict(zip(columns, row)) for row in rows]


@st.cache_resource
def ensure_table_exists():
    try:
        _execute_sql(f"SELECT 1 FROM {TABLE} LIMIT 1")
        return
    except Exception:
        pass
    _execute_sql(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            query_id         STRING NOT NULL,
            user_email       STRING NOT NULL,
            title            STRING NOT NULL,
            description      STRING,
            sql_text         STRING NOT NULL,
            original_prompt  STRING,
            genie_space      STRING,
            display_type     STRING NOT NULL,
            display_config   STRING,
            category         STRING,
            is_shared        BOOLEAN,
            is_default       BOOLEAN,
            refresh_schedule STRING,
            last_refreshed_at TIMESTAMP,
            last_result_json STRING,
            created_at       TIMESTAMP NOT NULL,
            updated_at       TIMESTAMP NOT NULL,
            sort_order       INT
        ) USING DELTA
    """)


def _escape(val):
    if val is None:
        return "NULL"
    return str(val).replace("\\", "\\\\").replace("'", "''")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- CRUD Operations ---


def get_user_queries(user_email):
    """Get all saved queries for a user (personal + shared)."""
    ensure_table_exists()
    email_escaped = _escape(user_email)
    query = f"""
        SELECT * FROM {TABLE}
        WHERE user_email = '{email_escaped}' OR is_shared = true
        ORDER BY sort_order ASC, updated_at DESC
    """
    return _execute_sql_dicts(query)


def get_query_by_id(query_id):
    """Get a single saved query by ID."""
    ensure_table_exists()
    results = _execute_sql_dicts(
        f"SELECT * FROM {TABLE} WHERE query_id = '{_escape(query_id)}'"
    )
    return results[0] if results else None


def save_query(user_email, title, sql_text, display_type="table",
               description=None, original_prompt=None, genie_space=None,
               category=None, display_config=None):
    """Save a new query. Returns the query_id."""
    ensure_table_exists()
    query_id = str(uuid.uuid4())
    now = _now_iso()

    _execute_sql(f"""
        INSERT INTO {TABLE}
        (query_id, user_email, title, description, sql_text, original_prompt,
         genie_space, display_type, display_config, category, is_shared, is_default,
         refresh_schedule, last_refreshed_at, last_result_json, created_at, updated_at, sort_order)
        VALUES (
            '{_escape(query_id)}',
            '{_escape(user_email)}',
            '{_escape(title)}',
            {f"'{_escape(description)}'" if description else "NULL"},
            '{_escape(sql_text)}',
            {f"'{_escape(original_prompt)}'" if original_prompt else "NULL"},
            {f"'{_escape(genie_space)}'" if genie_space else "NULL"},
            '{_escape(display_type)}',
            {f"'{_escape(display_config)}'" if display_config else "NULL"},
            {f"'{_escape(category)}'" if category else "NULL"},
            false,
            false,
            'manual',
            NULL,
            NULL,
            '{now}',
            '{now}',
            0
        )
    """)
    return query_id


def update_query(query_id, **kwargs):
    """Update fields on a saved query."""
    ensure_table_exists()
    allowed_fields = {
        "title", "description", "sql_text", "display_type", "display_config",
        "category", "is_shared", "sort_order", "refresh_schedule",
    }
    sets = []
    for key, val in kwargs.items():
        if key not in allowed_fields:
            continue
        if isinstance(val, bool):
            sets.append(f"{key} = {str(val).lower()}")
        elif val is None:
            sets.append(f"{key} = NULL")
        else:
            sets.append(f"{key} = '{_escape(val)}'")

    if not sets:
        return
    sets.append(f"updated_at = '{_now_iso()}'")
    set_clause = ", ".join(sets)
    _execute_sql(f"UPDATE {TABLE} SET {set_clause} WHERE query_id = '{_escape(query_id)}'")


def delete_query(query_id):
    """Delete a saved query."""
    ensure_table_exists()
    _execute_sql(f"DELETE FROM {TABLE} WHERE query_id = '{_escape(query_id)}'")


def refresh_query(query_id):
    """Execute the saved SQL and update the cached result. Returns (columns, rows)."""
    ensure_table_exists()
    record = get_query_by_id(query_id)
    if not record:
        raise ValueError(f"Query {query_id} not found")

    sql_text = record["sql_text"]
    columns, rows = _execute_sql(sql_text)

    cached_rows = rows[:MAX_CACHED_ROWS]
    result_json = json.dumps({"columns": columns, "rows": cached_rows})
    now = _now_iso()

    _execute_sql(f"""
        UPDATE {TABLE}
        SET last_refreshed_at = '{now}',
            last_result_json = '{_escape(result_json)}',
            updated_at = '{now}'
        WHERE query_id = '{_escape(query_id)}'
    """)

    return columns, rows


def get_cached_result(record):
    """Parse the cached result JSON from a query record."""
    raw = record.get("last_result_json")
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
        return data.get("columns", []), data.get("rows", [])
    except (json.JSONDecodeError, TypeError):
        return None, None


# --- Default Population ---


def populate_defaults(user_email):
    """Insert default queries for a new user. Returns count inserted."""
    ensure_table_exists()
    existing = _execute_sql_dicts(
        f"SELECT query_id FROM {TABLE} WHERE user_email = '{_escape(user_email)}'"
    )
    if existing:
        return 0

    count = 0
    now = _now_iso()
    for i, default in enumerate(REPORT_DEFAULTS):
        query_id = str(uuid.uuid4())
        _execute_sql(f"""
            INSERT INTO {TABLE}
            (query_id, user_email, title, description, sql_text, original_prompt,
             genie_space, display_type, display_config, category, is_shared, is_default,
             refresh_schedule, last_refreshed_at, last_result_json, created_at, updated_at, sort_order)
            VALUES (
                '{_escape(query_id)}',
                '{_escape(user_email)}',
                '{_escape(default["title"])}',
                '{_escape(default.get("description", ""))}',
                '{_escape(default["sql_text"])}',
                NULL,
                NULL,
                '{_escape(default["display_type"])}',
                NULL,
                '{_escape(default.get("category", ""))}',
                false,
                true,
                'manual',
                NULL,
                NULL,
                '{now}',
                '{now}',
                {i}
            )
        """)
        count += 1

    return count


# --- Display Type Detection ---


def detect_display_type(columns, rows):
    """Auto-detect the best display type based on result shape."""
    if not columns or not rows:
        return "table"

    num_cols = len(columns)
    num_rows = len(rows)

    if num_rows == 1 and num_cols == 1:
        return "metric"

    has_date_col = any(
        "date" in c.lower() or "month" in c.lower() or "week" in c.lower()
        for c in columns
    )
    has_numeric = num_cols >= 2

    if has_date_col and has_numeric and num_rows > 1:
        return "line_chart"

    if num_cols == 2 and num_rows <= 15 and not has_date_col:
        return "bar_chart"

    return "table"
