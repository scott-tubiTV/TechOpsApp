"""Persistence layer for dashboards in Argo."""

import uuid

import streamlit as st

from saved_queries_store import (
    _execute_sql,
    _execute_sql_dicts,
    _escape,
    _now_iso,
    TABLE as QUERIES_TABLE,
)

TABLE = "core_dev.techops.argo_dashboards"

DASHBOARD_DEFAULTS = [
    {
        "name": "Weekly Ops Review",
        "color": "#a855f7",
        "description": "Key metrics for Monday leadership walkthroughs.",
        "queries": ["ABF Backlog", "Reviews by Team (MTD)", "Imports This Month"],
    },
    {
        "name": "Redeliveries",
        "color": "#f59e0b",
        "description": "Open redelivery health.",
        "queries": ["Open Redeliveries", "Redeliveries by Modality"],
    },
    {
        "name": "Pipeline Health",
        "color": "#2dd4bf",
        "description": "Import & policy trends.",
        "queries": ["Weekly Import Trend", "Titles Expiring This Month"],
    },
    {
        "name": "ABF Throughput",
        "color": "#3b82f6",
        "description": "Queue depth & team performance.",
        "queries": ["ABF Queue by Team"],
    },
]


@st.cache_resource
def ensure_dashboards_table():
    try:
        _execute_sql(f"SELECT 1 FROM {TABLE} LIMIT 1")
        return
    except Exception:
        pass
    _execute_sql(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            dashboard_id  STRING NOT NULL,
            user_email    STRING NOT NULL,
            name          STRING NOT NULL,
            description   STRING,
            color         STRING,
            is_shared     BOOLEAN,
            created_at    TIMESTAMP NOT NULL,
            updated_at    TIMESTAMP NOT NULL,
            sort_order    INT
        ) USING DELTA
    """)


def get_user_dashboards(user_email):
    """Get dashboards owned by user + shared dashboards from others."""
    ensure_dashboards_table()
    email_escaped = _escape(user_email)
    return _execute_sql_dicts(f"""
        SELECT * FROM {TABLE}
        WHERE user_email = '{email_escaped}' OR is_shared = true
        ORDER BY sort_order ASC, updated_at DESC
    """)


def get_dashboard_by_id(dashboard_id):
    ensure_dashboards_table()
    results = _execute_sql_dicts(
        f"SELECT * FROM {TABLE} WHERE dashboard_id = '{_escape(dashboard_id)}'"
    )
    return results[0] if results else None


def create_dashboard(user_email, name, description=None, color="#a855f7"):
    """Create a new dashboard. Returns dashboard_id."""
    ensure_dashboards_table()
    dashboard_id = str(uuid.uuid4())
    now = _now_iso()
    _execute_sql(f"""
        INSERT INTO {TABLE}
        (dashboard_id, user_email, name, description, color, is_shared, created_at, updated_at, sort_order)
        VALUES (
            '{_escape(dashboard_id)}',
            '{_escape(user_email)}',
            '{_escape(name)}',
            {f"'{_escape(description)}'" if description else "NULL"},
            '{_escape(color)}',
            false,
            '{now}',
            '{now}',
            0
        )
    """)
    return dashboard_id


def update_dashboard(dashboard_id, **kwargs):
    """Update dashboard fields."""
    ensure_dashboards_table()
    allowed = {"name", "description", "color", "is_shared", "sort_order"}
    sets = []
    for key, val in kwargs.items():
        if key not in allowed:
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
    _execute_sql(
        f"UPDATE {TABLE} SET {', '.join(sets)} WHERE dashboard_id = '{_escape(dashboard_id)}'"
    )


def delete_dashboard(dashboard_id):
    """Delete a dashboard and unlink its queries."""
    ensure_dashboards_table()
    _execute_sql(
        f"UPDATE {QUERIES_TABLE} SET dashboard_id = NULL WHERE dashboard_id = '{_escape(dashboard_id)}'"
    )
    _execute_sql(f"DELETE FROM {TABLE} WHERE dashboard_id = '{_escape(dashboard_id)}'")


def get_dashboard_queries(dashboard_id):
    """Get all saved queries belonging to a dashboard."""
    from saved_queries_store import get_cached_result
    results = _execute_sql_dicts(f"""
        SELECT * FROM {QUERIES_TABLE}
        WHERE dashboard_id = '{_escape(dashboard_id)}'
        ORDER BY sort_order ASC, updated_at DESC
    """)
    return results


def populate_default_dashboards(user_email):
    """Seed default dashboards for a new user. Returns count created."""
    ensure_dashboards_table()
    existing = _execute_sql_dicts(
        f"SELECT dashboard_id FROM {TABLE} WHERE user_email = '{_escape(user_email)}'"
    )
    if existing:
        return 0

    count = 0
    now = _now_iso()
    for i, default in enumerate(DASHBOARD_DEFAULTS):
        dashboard_id = str(uuid.uuid4())
        _execute_sql(f"""
            INSERT INTO {TABLE}
            (dashboard_id, user_email, name, description, color, is_shared, created_at, updated_at, sort_order)
            VALUES (
                '{_escape(dashboard_id)}',
                '{_escape(user_email)}',
                '{_escape(default["name"])}',
                '{_escape(default["description"])}',
                '{_escape(default["color"])}',
                false,
                '{now}',
                '{now}',
                {i}
            )
        """)
        count += 1

        query_titles = default.get("queries", [])
        if query_titles:
            title_conditions = " OR ".join(
                f"title = '{_escape(t)}'" for t in query_titles
            )
            _execute_sql(f"""
                UPDATE {QUERIES_TABLE}
                SET dashboard_id = '{_escape(dashboard_id)}', updated_at = '{now}'
                WHERE user_email = '{_escape(user_email)}'
                  AND ({title_conditions})
                  AND (dashboard_id IS NULL OR dashboard_id = '')
            """)

    return count
