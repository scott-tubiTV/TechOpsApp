"""Conversation persistence layer for Argo chat history."""

import json
import time
import uuid
from datetime import datetime, timezone

import streamlit as st
from databricks.sdk import WorkspaceClient

TABLE = "core_dev.techops.argo_conversations"
WAREHOUSE_ID = "a6b9541289d75c6e"
MAX_DF_ROWS_STORED = 5


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
    return [dict(zip(columns, row)) for row in rows]


@st.cache_resource
def ensure_table_exists():
    _execute_sql(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            conversation_id  STRING NOT NULL,
            user_email       STRING NOT NULL,
            title            STRING NOT NULL,
            space_name       STRING,
            messages_json    STRING NOT NULL,
            created_at       TIMESTAMP NOT NULL,
            updated_at       TIMESTAMP NOT NULL,
            shared_with      STRING,
            is_archived      BOOLEAN,
            message_count    INT NOT NULL
        ) USING DELTA
    """)


def _escape(val):
    if val is None:
        return "NULL"
    return str(val).replace("\\", "\\\\").replace("'", "''")


def _generate_title(messages):
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            title = msg["content"].strip().replace("\n", " ")
            return title[:50] + ("..." if len(title) > 50 else "")
    return "Untitled conversation"


def _sanitize_messages(messages):
    sanitized = []
    for msg in messages:
        m = {k: v for k, v in msg.items() if not k.startswith("_")}
        if m.get("dataframe"):
            df = m["dataframe"]
            m["dataframe"] = {
                "columns": df.get("columns", []),
                "data": df.get("data", [])[:MAX_DF_ROWS_STORED],
                "_truncated": len(df.get("data", [])) > MAX_DF_ROWS_STORED,
                "_total_rows": len(df.get("data", [])),
            }
        sanitized.append(m)
    return sanitized


def save_conversation(user_email, messages, space_name, conversation_id=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    title = _generate_title(messages)
    sanitized = _sanitize_messages(messages)
    messages_json = json.dumps(sanitized, default=str)
    msg_count = len(messages)

    if conversation_id is None:
        conversation_id = uuid.uuid4().hex

    _execute_sql(f"""
        MERGE INTO {TABLE} AS target
        USING (SELECT '{_escape(conversation_id)}' AS conversation_id) AS source
        ON target.conversation_id = source.conversation_id
        WHEN MATCHED THEN UPDATE SET
            messages_json = '{_escape(messages_json)}',
            updated_at = '{now}',
            message_count = {msg_count},
            title = '{_escape(title)}'
        WHEN NOT MATCHED THEN INSERT
            (conversation_id, user_email, title, space_name, messages_json, created_at, updated_at, is_archived, message_count)
        VALUES
            ('{_escape(conversation_id)}', '{_escape(user_email)}',
             '{_escape(title)}', '{_escape(space_name)}',
             '{_escape(messages_json)}', '{now}', '{now}', FALSE, {msg_count})
    """)
    return conversation_id


@st.cache_data(ttl=30)
def list_conversations(user_email, search="", limit=50):
    where_parts = [
        f"(user_email = '{_escape(user_email)}' OR shared_with LIKE '%{_escape(user_email)}%')",
        "is_archived = FALSE",
    ]
    if search:
        where_parts.append(f"LOWER(title) LIKE '%{_escape(search.lower())}%'")

    where = " AND ".join(where_parts)
    return _execute_sql(f"""
        SELECT conversation_id, title, space_name, updated_at, message_count, user_email, shared_with
        FROM {TABLE}
        WHERE {where}
        ORDER BY updated_at DESC
        LIMIT {limit}
    """)


def load_conversation(conversation_id, requesting_email):
    rows = _execute_sql(f"""
        SELECT conversation_id, user_email, title, space_name, messages_json,
               created_at, updated_at, shared_with, message_count
        FROM {TABLE}
        WHERE conversation_id = '{_escape(conversation_id)}'
        LIMIT 1
    """)
    if not rows:
        return None
    row = rows[0]
    owner = row["user_email"]
    shared = row.get("shared_with") or ""
    if requesting_email != owner and requesting_email not in shared:
        return None
    row["messages"] = json.loads(row["messages_json"])
    return row


def share_conversation(conversation_id, share_with_email):
    rows = _execute_sql(f"""
        SELECT shared_with FROM {TABLE}
        WHERE conversation_id = '{_escape(conversation_id)}'
    """)
    if not rows:
        return
    current = rows[0].get("shared_with") or ""
    emails = [e.strip() for e in current.split(",") if e.strip()]
    if share_with_email not in emails:
        emails.append(share_with_email)
    new_shared = ",".join(emails)
    _execute_sql(f"""
        UPDATE {TABLE}
        SET shared_with = '{_escape(new_shared)}'
        WHERE conversation_id = '{_escape(conversation_id)}'
    """)


def archive_conversation(conversation_id):
    _execute_sql(f"""
        UPDATE {TABLE}
        SET is_archived = TRUE
        WHERE conversation_id = '{_escape(conversation_id)}'
    """)
