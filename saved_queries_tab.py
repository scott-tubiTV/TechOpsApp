"""Saved Queries / Reports tab for Argo."""

import json

import pandas as pd
import streamlit as st

from saved_queries_store import (
    get_user_queries,
    save_query,
    update_query,
    delete_query,
    refresh_query,
    populate_defaults,
    get_cached_result,
    detect_display_type,
    _decode_sql,
)


def _get_user_email():
    return st.session_state.get("user_email", "unknown@tubi.tv")


def _render_metric(columns, rows):
    if rows and rows[0]:
        val = rows[0][0]
        try:
            val = float(val)
            if val == int(val):
                val = f"{int(val):,}"
            else:
                val = f"{val:,.2f}"
        except (ValueError, TypeError):
            val = str(val)
        st.metric(label="", value=val)
    else:
        st.info("No data")


def _render_bar_chart(columns, rows):
    if not rows or len(columns) < 2:
        st.info("No data for chart")
        return
    df = pd.DataFrame(rows, columns=columns)
    df.iloc[:, 1] = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    st.bar_chart(df, x=columns[0], y=columns[1])


def _render_line_chart(columns, rows):
    if not rows or len(columns) < 2:
        st.info("No data for chart")
        return
    df = pd.DataFrame(rows, columns=columns)
    df.iloc[:, 1] = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    st.line_chart(df, x=columns[0], y=columns[1])


def _render_pie_chart(columns, rows):
    if not rows or len(columns) < 2:
        st.info("No data for chart")
        return
    df = pd.DataFrame(rows, columns=columns)
    df.iloc[:, 1] = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    st.bar_chart(df, x=columns[0], y=columns[1])


def _render_table(columns, rows):
    if not rows:
        st.info("No data")
        return
    df = pd.DataFrame(rows, columns=columns)
    st.dataframe(df, use_container_width=True, hide_index=True)


RENDERERS = {
    "metric": _render_metric,
    "bar_chart": _render_bar_chart,
    "line_chart": _render_line_chart,
    "pie_chart": _render_pie_chart,
    "table": _render_table,
}


def _render_report_card(record, idx):
    title = record.get("title", "Untitled")
    description = record.get("description", "")
    display_type = record.get("display_type", "table")
    query_id = record.get("query_id", "")
    last_refreshed = record.get("last_refreshed_at")
    is_default = record.get("is_default") in (True, "true", "1")

    refreshed_label = ""
    if last_refreshed:
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(last_refreshed.replace("Z", "+00:00"))
            refreshed_label = f'<div style="font-size:10px; color:#a49bb3; margin-top:4px;">Last refreshed: {ts.strftime("%b %d, %Y %I:%M %p")} UTC</div>'
        except Exception:
            refreshed_label = f'<div style="font-size:10px; color:#a49bb3; margin-top:4px;">Last refreshed: {last_refreshed}</div>'

    with st.container():
        st.markdown(f"""
        <div style="border:1px solid #eceaf2; border-radius:12px; padding:16px; margin-bottom:12px; background:#faf9fc;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                    <div style="font-weight:700; font-size:15px; color:#1b1626;">{title}</div>
                    <div style="font-size:12px; color:#8a8199; margin-top:2px;">{description}</div>
                    {refreshed_label}
                </div>
                <div style="font-size:10px; color:#a49bb3; text-transform:uppercase; letter-spacing:0.05em;">
                    {display_type.replace('_', ' ')}
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        columns, rows = get_cached_result(record)
        if columns and rows:
            renderer = RENDERERS.get(display_type, _render_table)
            renderer(columns, rows)
        elif last_refreshed:
            st.caption("Cached data unavailable — click Refresh")
        else:
            st.caption("Not yet refreshed — click Refresh to load data")

        col_refresh, col_edit, col_delete, col_spacer = st.columns([1, 1, 1, 3])
        with col_refresh:
            if st.button("🔄 Refresh", key=f"refresh_{query_id}_{idx}"):
                with st.spinner("Running query..."):
                    try:
                        columns, rows = refresh_query(query_id)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Query failed: {e}")
        with col_edit:
            if st.button("✏️ Edit", key=f"edit_{query_id}_{idx}"):
                st.session_state[f"_editing_{query_id}"] = True
                st.rerun()
        with col_delete:
            if not is_default:
                if st.button("🗑️", key=f"del_{query_id}_{idx}"):
                    delete_query(query_id)
                    st.rerun()

        if st.session_state.get(f"_editing_{query_id}"):
            _render_edit_form(record, idx)


def _render_edit_form(record, idx):
    query_id = record["query_id"]
    with st.form(key=f"edit_form_{query_id}_{idx}"):
        new_title = st.text_input("Title", value=record.get("title", ""))
        new_desc = st.text_input("Description", value=record.get("description", ""))
        new_sql = st.text_area("SQL", value=_decode_sql(record.get("sql_text", "")), height=120)
        new_display = st.selectbox(
            "Display type",
            ["metric", "bar_chart", "line_chart", "pie_chart", "table"],
            index=["metric", "bar_chart", "line_chart", "pie_chart", "table"].index(
                record.get("display_type", "table")
            ),
        )
        new_category = st.text_input("Category", value=record.get("category", ""))

        col_save, col_cancel = st.columns(2)
        with col_save:
            submitted = st.form_submit_button("Save Changes", use_container_width=True)
        with col_cancel:
            cancelled = st.form_submit_button("Cancel", use_container_width=True)

        if submitted:
            update_query(
                query_id,
                title=new_title,
                description=new_desc,
                sql_text=new_sql,
                display_type=new_display,
                category=new_category,
            )
            st.session_state.pop(f"_editing_{query_id}", None)
            st.rerun()
        if cancelled:
            st.session_state.pop(f"_editing_{query_id}", None)
            st.rerun()


def _render_add_form():
    with st.form(key="add_report_form"):
        st.markdown("**Add a new report**")
        title = st.text_input("Title")
        description = st.text_input("Description")
        sql_text = st.text_area("SQL Query", height=120)
        display_type = st.selectbox(
            "Display type",
            ["table", "metric", "bar_chart", "line_chart", "pie_chart"],
        )
        category = st.text_input("Category (optional)")

        submitted = st.form_submit_button("Save Report", use_container_width=True)
        if submitted:
            if not title or not sql_text:
                st.error("Title and SQL are required.")
            else:
                save_query(
                    user_email=_get_user_email(),
                    title=title,
                    sql_text=sql_text,
                    display_type=display_type,
                    description=description,
                    category=category,
                )
                st.session_state._show_add_form = False
                st.rerun()


def render_saved_queries_tab():
    """Main entry point — renders the full Reports tab."""
    user_email = _get_user_email()

    st.markdown("""
    <div style="padding: 4px 0 16px;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.02em; color:#1b1626;">
            Reports
        </div>
        <div style="font-size:13px; color:#8a8199; margin-top:4px;">
            Saved queries and metrics that refresh on demand. Add your own or save results from chat.
        </div>
    </div>
    """, unsafe_allow_html=True)

    populate_defaults(user_email)

    header_col1, header_col2, header_col3 = st.columns([2, 2, 1])
    with header_col1:
        category_filter = st.selectbox(
            "Filter by category",
            ["All"] + sorted(set(
                r.get("category", "")
                for r in get_user_queries(user_email)
                if r.get("category")
            )),
            label_visibility="collapsed",
        )
    with header_col2:
        if st.button("🔄 Refresh All", use_container_width=True):
            queries = get_user_queries(user_email)
            progress = st.progress(0)
            for i, q in enumerate(queries):
                try:
                    refresh_query(q["query_id"])
                except Exception:
                    pass
                progress.progress((i + 1) / len(queries))
            progress.empty()
            st.rerun()
    with header_col3:
        if st.button("➕ Add Report", use_container_width=True):
            st.session_state._show_add_form = not st.session_state.get("_show_add_form", False)
            st.rerun()

    if st.session_state.get("_show_add_form"):
        _render_add_form()

    queries = get_user_queries(user_email)

    if category_filter != "All":
        queries = [q for q in queries if q.get("category") == category_filter]

    if not queries:
        st.info("No reports yet. Click **Add Report** to create one, or save a query result from chat.")
        return

    for idx, record in enumerate(queries):
        _render_report_card(record, idx)
