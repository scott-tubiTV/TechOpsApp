"""Dashboards tab for Argo — collections of saved query cards."""

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from dashboards_store import (
    get_user_dashboards,
    get_dashboard_by_id,
    create_dashboard,
    update_dashboard,
    delete_dashboard,
    get_dashboard_queries,
    populate_default_dashboards,
)
from saved_queries_store import (
    save_query,
    update_query,
    delete_query,
    refresh_query,
    get_cached_result,
    populate_defaults,
    detect_display_type,
    _decode_sql,
)


COLOR_PRESETS = ["#a855f7", "#3b82f6", "#2dd4bf", "#f59e0b", "#f472b6", "#ef4444", "#6366f1"]


def _get_user_email():
    return st.session_state.get("user_email", "unknown@tubi.tv")


def _relative_time(timestamp_str):
    if not timestamp_str:
        return "Never"
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "Just now"
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hr{'s' if hours > 1 else ''} ago"
        days = hours // 24
        if days == 1:
            return "Yesterday"
        if days < 7:
            return f"{days} days ago"
        return ts.strftime("%b %d")
    except Exception:
        return str(timestamp_str)[:10]


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
        st.markdown(f"""
        <div style="font-family:'JetBrains Mono',monospace;font-size:28px;font-weight:600;color:#1b1626;line-height:1;">{val}</div>
        """, unsafe_allow_html=True)
    else:
        st.caption("No data")


def _render_bar_chart(columns, rows):
    if not rows or len(columns) < 2:
        st.caption("No data for chart")
        return
    df = pd.DataFrame(rows, columns=columns)
    for c in columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index(columns[0])
    st.bar_chart(df, height=160)


def _render_line_chart(columns, rows):
    if not rows or len(columns) < 2:
        st.caption("No data for chart")
        return
    df = pd.DataFrame(rows, columns=columns)
    for c in columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index(columns[0])
    st.line_chart(df, height=160)


def _render_table(columns, rows):
    if not rows:
        st.caption("No data")
        return
    df = pd.DataFrame(rows, columns=columns)
    st.dataframe(df, use_container_width=True, hide_index=True, height=180)


RENDERERS = {
    "metric": _render_metric,
    "bar_chart": _render_bar_chart,
    "line_chart": _render_line_chart,
    "pie_chart": _render_bar_chart,
    "table": _render_table,
}


# === View 1: Dashboard List ===

def _render_dashboard_list(user_email):
    dashboards = get_user_dashboards(user_email)

    header_col1, header_col2 = st.columns([5, 2])
    with header_col1:
        st.markdown("""
        <div style="font-family:'Space Grotesk',sans-serif;font-size:22px;font-weight:700;letter-spacing:-0.02em;color:#1b1626;">Dashboards</div>
        <div style="font-size:13px;color:#8a8199;margin-top:4px;margin-bottom:16px;">Collections of saved query results you re-run on demand.</div>
        """, unsafe_allow_html=True)
    with header_col2:
        if st.button("+ New dashboard", use_container_width=True, type="primary"):
            st.session_state.dash_view = "form"
            st.session_state.editing_dashboard_id = None
            st.rerun()

    if not dashboards:
        st.info("No dashboards yet. Create one to start organizing your saved queries.")
        return

    cols = st.columns(2)
    for idx, dash in enumerate(dashboards):
        col = cols[idx % 2]
        with col:
            color = dash.get("color", "#a855f7")
            name = dash.get("name", "Untitled")
            desc = dash.get("description", "")
            owner = dash.get("user_email", "").split("@")[0]
            is_shared = dash.get("is_shared") in (True, "true", "1")
            updated = _relative_time(dash.get("updated_at"))

            shared_badge = ' <span style="color:#7c3aed;font-size:10px;font-weight:600;">(Shared)</span>' if is_shared and dash.get("user_email") != user_email else ""

            st.markdown(f"""
            <div style="border:1px solid #e7e3ee;background:#fff;border-radius:13px;padding:17px 18px;margin-bottom:14px;min-height:140px;">
                <div style="display:flex;align-items:center;gap:9px;margin-bottom:8px;">
                    <span style="width:9px;height:9px;border-radius:3px;flex:0 0 9px;background:{color};"></span>
                    <div style="font-size:14.5px;font-weight:700;color:#1b1626;">{name}{shared_badge}</div>
                </div>
                <div style="font-size:12.5px;color:#8a8199;line-height:1.55;margin-bottom:14px;min-height:32px;">{desc}</div>
                <div style="display:flex;align-items:center;justify-content:space-between;font-size:11.5px;color:#a49bb3;border-top:1px solid #f2f0f7;padding-top:11px;">
                    <span>{owner}</span>
                    <span>Updated {updated}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            if st.button("Open", key=f"open_dash_{dash['dashboard_id']}_{idx}", use_container_width=True):
                st.session_state.dash_view = "detail"
                st.session_state.active_dashboard_id = dash["dashboard_id"]
                st.rerun()


# === View 2: Dashboard Detail ===

def _render_dashboard_detail(user_email):
    dashboard_id = st.session_state.get("active_dashboard_id")
    if not dashboard_id:
        st.session_state.dash_view = "list"
        st.rerun()
        return

    dash = get_dashboard_by_id(dashboard_id)
    if not dash:
        st.error("Dashboard not found.")
        st.session_state.dash_view = "list"
        return

    name = dash.get("name", "Untitled")
    color = dash.get("color", "#a855f7")
    owner = dash.get("user_email", "").split("@")[0]
    is_owner = dash.get("user_email") == user_email
    is_shared = dash.get("is_shared") in (True, "true", "1")

    # Breadcrumb
    bc_col1, bc_col2 = st.columns([5, 3])
    with bc_col1:
        if st.button("← Dashboards", key="back_to_list"):
            st.session_state.dash_view = "list"
            st.rerun()
        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:9px;margin-top:4px;">
            <span style="width:10px;height:10px;border-radius:3px;background:{color};"></span>
            <span style="font-family:'Space Grotesk',sans-serif;font-size:20px;font-weight:700;color:#1b1626;">{name}</span>
        </div>
        <div style="font-size:12px;color:#a49bb3;margin-top:4px;margin-bottom:16px;">{owner}</div>
        """, unsafe_allow_html=True)
    with bc_col2:
        btn_cols = st.columns(3)
        with btn_cols[0]:
            if is_owner:
                share_label = "Unshare" if is_shared else "Share"
                if st.button(share_label, key="toggle_share", use_container_width=True):
                    update_dashboard(dashboard_id, is_shared=not is_shared)
                    st.rerun()
        with btn_cols[1]:
            if st.button("Refresh All", key="refresh_all_dash", use_container_width=True):
                queries = get_dashboard_queries(dashboard_id)
                if queries:
                    progress = st.progress(0)
                    for i, q in enumerate(queries):
                        try:
                            refresh_query(q["query_id"])
                        except Exception:
                            pass
                        progress.progress((i + 1) / len(queries))
                    progress.empty()
                    st.rerun()
        with btn_cols[2]:
            if is_owner:
                if st.button("Edit", key="edit_dash", use_container_width=True):
                    st.session_state.dash_view = "form"
                    st.session_state.editing_dashboard_id = dashboard_id
                    st.rerun()

    # Query cards
    queries = get_dashboard_queries(dashboard_id)

    if not queries:
        st.markdown("""
        <div style="border:1px dashed #d9d2e6;border-radius:13px;padding:40px 18px;text-align:center;margin-top:12px;">
            <div style="font-size:20px;color:#c4bcd2;">+</div>
            <div style="font-size:13px;font-weight:600;color:#6d647e;margin-top:8px;">No queries yet</div>
            <div style="font-size:12px;color:#a49bb3;margin-top:4px;">Ask Argo anything, then choose <strong style="color:#7c3aed;">Save to dashboard</strong> on the result.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    cols = st.columns(2)
    for idx, record in enumerate(queries):
        col = cols[idx % 2]
        with col:
            _render_query_card(record, idx, is_owner)

    # Add query placeholder
    col_placeholder = cols[len(queries) % 2]
    with col_placeholder:
        st.markdown("""
        <div style="border:1px dashed #d9d2e6;border-radius:13px;padding:26px 18px;text-align:center;min-height:180px;display:flex;flex-direction:column;align-items:center;justify-content:center;">
            <div style="font-size:20px;color:#c4bcd2;">+</div>
            <div style="font-size:13px;font-weight:600;color:#6d647e;margin-top:8px;">Add a query</div>
            <div style="font-size:11.5px;color:#a49bb3;line-height:1.5;max-width:200px;margin-top:4px;">Ask Argo anything, then choose <strong style="color:#7c3aed;">Save to dashboard</strong> on the result.</div>
        </div>
        """, unsafe_allow_html=True)


def _render_query_card(record, idx, is_owner):
    title = record.get("title", "Untitled")
    display_type = record.get("display_type", "table")
    query_id = record.get("query_id", "")
    genie_space = record.get("genie_space", "")
    last_refreshed = record.get("last_refreshed_at")

    genie_color = "#a855f7"
    if genie_space:
        space_colors = {
            "Content Metrics": "#a855f7",
            "Redeliveries": "#f59e0b",
            "Partner Detail": "#3b82f6",
            "Content Library": "#2dd4bf",
        }
        genie_color = space_colors.get(genie_space, "#a855f7")

    genie_badge = ""
    if genie_space:
        genie_badge = f'<div style="display:inline-flex;align-items:center;gap:6px;margin-top:8px;font-size:11px;font-weight:600;color:#6d647e;background:#f7f5fb;border:1px solid #eceaf2;padding:3px 9px;border-radius:20px;"><span style="width:6px;height:6px;border-radius:50%;background:{genie_color};"></span>{genie_space}</div>'

    with st.container(border=True):
        # Card header
        st.markdown(f"""
        <div style="margin:-1rem -1rem 0.75rem -1rem;padding:14px 17px 10px;border-bottom:1px solid #f2f0f7;">
            <div style="font-size:13.5px;font-weight:700;color:#1b1626;line-height:1.4;">{title}</div>
            {genie_badge}
        </div>
        """, unsafe_allow_html=True)

        # Card body — visualization
        columns, rows = get_cached_result(record)
        if columns and rows:
            renderer = RENDERERS.get(display_type, _render_table)
            renderer(columns, rows)
        else:
            st.caption("Not yet refreshed — click Refresh")

        # Card footer
        footer_cols = st.columns([2, 1, 1, 1])
        with footer_cols[0]:
            st.caption(f"↻ {_relative_time(last_refreshed)}")
        with footer_cols[1]:
            if st.button("Refresh", key=f"ref_{query_id}_{idx}"):
                with st.spinner(""):
                    try:
                        refresh_query(query_id)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e)[:80])
        with footer_cols[2]:
            if columns and rows:
                df = pd.DataFrame(rows, columns=columns)
                csv = df.to_csv(index=False)
                st.download_button("CSV", csv, f"{title}.csv", "text/csv", key=f"csv_{query_id}_{idx}")
        with footer_cols[3]:
            if is_owner:
                if st.button("Remove", key=f"rm_{query_id}_{idx}"):
                    update_query(query_id, dashboard_id=None)
                    st.rerun()


# === View 3: Dashboard Form ===

def _render_dashboard_form(user_email):
    editing_id = st.session_state.get("editing_dashboard_id")
    existing = None
    if editing_id:
        existing = get_dashboard_by_id(editing_id)

    st.markdown(f"""
    <div style="font-family:'Space Grotesk',sans-serif;font-size:20px;font-weight:700;color:#1b1626;margin-bottom:16px;">
        {'Edit' if existing else 'New'} Dashboard
    </div>
    """, unsafe_allow_html=True)

    with st.form("dashboard_form"):
        name = st.text_input("Name", value=existing["name"] if existing else "")
        description = st.text_input("Description", value=existing.get("description", "") if existing else "")

        color_labels = {c: f"⬤ {c}" for c in COLOR_PRESETS}
        current_color = existing.get("color", "#a855f7") if existing else "#a855f7"
        color_idx = COLOR_PRESETS.index(current_color) if current_color in COLOR_PRESETS else 0
        color = st.selectbox("Color", COLOR_PRESETS, index=color_idx, format_func=lambda c: c)

        col_save, col_cancel, col_delete = st.columns([2, 2, 1])
        with col_save:
            submitted = st.form_submit_button("Save", use_container_width=True, type="primary")
        with col_cancel:
            cancelled = st.form_submit_button("Cancel", use_container_width=True)
        with col_delete:
            if existing:
                deleted = st.form_submit_button("Delete", use_container_width=True)
            else:
                deleted = False

        if submitted and name:
            if existing:
                update_dashboard(editing_id, name=name, description=description, color=color)
            else:
                create_dashboard(user_email, name, description, color)
            st.session_state.dash_view = "list"
            st.rerun()
        if cancelled:
            st.session_state.dash_view = "list" if not editing_id else "detail"
            st.rerun()
        if deleted and existing:
            delete_dashboard(editing_id)
            st.session_state.dash_view = "list"
            st.session_state.active_dashboard_id = None
            st.rerun()


# === Main Entry Point ===

def render_dashboards_tab():
    """Main entry point — renders the Dashboards tab."""
    user_email = _get_user_email()

    # Seed defaults
    populate_defaults(user_email)
    populate_default_dashboards(user_email)

    # Initialize view state
    if "dash_view" not in st.session_state:
        st.session_state.dash_view = "list"

    view = st.session_state.dash_view

    if view == "detail":
        _render_dashboard_detail(user_email)
    elif view == "form":
        _render_dashboard_form(user_email)
    else:
        _render_dashboard_list(user_email)
