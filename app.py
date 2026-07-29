"""Argo — TechOps content metrics chat interface powered by Databricks Genie."""

import os
import pandas as pd
import streamlit as st
from genie_client import GenieClient
from tools.sony_matcher import render_content_id_matcher

ADMIN_EMAILS = [
    "swhitney@tubi.tv",
]

TOOL_USERS = [
    "swhitney@tubi.tv",
    "caitlinlittle@tubi.tv",
    "marianladiona@tubi.tv",
]

TOOLS = {
    "Content ID Matcher": {
        "icon": "🔍",
        "description": "Look up Tubi content IDs by title from the live database",
        "renderer": render_content_id_matcher,
    },
}

GENIE_SPACES = {
    "Content Metrics": {
        "id": os.environ.get("GENIE_SPACE_TECHOPS", "01f1808d550e12fb9bd0578798518174"),
        "description": "Ops metrics: policy snapshots, avails status, imports, images, partners, QA/ABF, workload, content refresh",
        "keywords": ["policy", "avail", "import", "image", "qa", "abf", "workload", "snapshot", "metric", "weekly", "monthly", "refresh", "gate", "published", "curation"],
        "color": "#a855f7",
    },
    "Redeliveries": {
        "id": os.environ.get("GENIE_SPACE_REDELIVERIES", "01f18b7f50db1b3d851f29a3c3d66295"),
        "description": "Redelivery detail: per-title breakdown by reason, modality, age, partner, and dismissal status",
        "keywords": ["redelivery", "redeliver", "dismissed", "modality", "video file", "subtitle file", "image file", "overdue", "backlog", "oldest", "reason", "partner"],
        "color": "#f59e0b",
    },
    "Dupe Checker V2": {
        "id": os.environ.get("GENIE_SPACE_DUPE_CHECKER", "01f122f4e2921b7a9c28ef03d0812ee6"),
        "description": "Duplicate title detection: upload CSV avails to check for existing titles and conflicts",
        "keywords": ["dupe", "duplicate", "conflict", "csv", "upload", "check", "avails file", "overlap", "territory"],
        "color": "#f472b6",
    },
}

SUGGESTION_CARDS = [
    "Show ops workload breakdown this week",
    "How many avails are pending review?",
    "Partner summary by title count",
    "Which partners have the most overdue redeliveries?",
]

st.set_page_config(
    page_title="Argo | TechOps",
    page_icon="✦",
    layout="wide",
)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

    .stApp header {background-color: #faf9fc; border-bottom: 1px solid #eceaf2;}
    [data-testid="stSidebar"] {
        background: #f3f0f8;
        border-right: 1px solid #e7e3ee;
    }
    [data-testid="stSidebar"] * {color: #4b4458 !important;}
    [data-testid="stSidebar"] .stMarkdown h1,
    [data-testid="stSidebar"] .stMarkdown h2,
    [data-testid="stSidebar"] .stMarkdown h3 {color: #1b1626 !important;}

    [data-testid="stChatMessage"] {
        border-radius: 12px;
        margin-bottom: 10px;
        background: #ffffff;
        border: 1px solid #e7e3ee;
    }
    .stChatInput > div {
        border: 1px solid #ddd6ea !important;
        border-radius: 14px !important;
        background: #ffffff !important;
        box-shadow: 0 8px 24px rgba(90,60,140,0.06);
    }
    .stChatInput > div:focus-within {
        border-color: #7c3aed !important;
        box-shadow: 0 8px 24px rgba(124,58,237,0.12) !important;
    }
    .stDataFrame {
        border-radius: 12px;
        border: 1px solid #e7e3ee;
        box-shadow: 0 4px 16px rgba(90,60,140,0.04);
    }
    div[data-testid="stExpander"] {
        border: 1px solid #e7e3ee;
        border-radius: 12px;
        background: #faf9fc;
    }
    .stButton > button {
        border: 1px solid #e2ddec;
        border-radius: 10px;
        font-weight: 600;
        transition: all 0.2s ease;
    }
    .stButton > button:hover {
        border-color: #7c3aed;
        color: #7c3aed;
        box-shadow: 0 4px 12px rgba(124,58,237,0.1);
    }
    h1, h2, h3 {font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em;}
    p, span, div {font-family: 'Manrope', sans-serif;}
    code, pre {font-family: 'JetBrains Mono', monospace !important;}

    .suggestion-card {
        border: 1px solid #e7e3ee;
        background: #ffffff;
        border-radius: 11px;
        padding: 14px 16px;
        font-size: 14px;
        color: #3b3448;
        cursor: pointer;
        transition: all 0.2s ease;
    }
    .suggestion-card:hover {
        border-color: #7c3aed;
        box-shadow: 0 4px 16px rgba(124,58,237,0.08);
    }
    .routing-badge {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        background: #f3ecfd;
        border: 1px solid #e3d3fa;
        color: #7c3aed;
        font-size: 12px;
        font-weight: 600;
        padding: 4px 10px;
        border-radius: 20px;
        margin-bottom: 8px;
    }
    .routing-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        display: inline-block;
    }
    .space-item {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 12px;
        border-radius: 9px;
        font-size: 13px;
        color: #3b3448;
    }
    .space-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        display: inline-block;
        flex-shrink: 0;
    }
    .status-indicator {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: #8a8199;
    }
    .status-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: #2dd4bf;
        animation: pulse 2s infinite;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_genie_client():
    return GenieClient()


def get_user_email():
    # Databricks Apps exposes logged-in user via st.experimental_user
    try:
        user_info = st.experimental_user
        if user_info and user_info.get("email"):
            return user_info["email"]
    except Exception:
        pass
    headers = st.context.headers
    email = headers.get("X-Forwarded-Email") or headers.get("X-Forwarded-Preferred-Username")
    if email:
        return email
    for h in ["x-forwarded-email", "x-forwarded-preferred-username", "X-Databricks-User-Email"]:
        val = headers.get(h)
        if val:
            return val
    try:
        client = get_genie_client()
        me = client.w.current_user.me()
        return me.user_name or me.display_name or "unknown"
    except Exception:
        return "unknown"


def is_admin(email):
    return email and email.lower() in [e.lower() for e in ADMIN_EMAILS]


def has_tool_access(email):
    return email and email.lower() in [e.lower() for e in TOOL_USERS]


def get_user_display_name(email):
    if email and "@" in email:
        name = email.split("@")[0]
        parts = name.replace(".", " ").replace("_", " ").split()
        return " ".join(p.capitalize() for p in parts)
    return "User"


def get_user_initials(email):
    name = get_user_display_name(email)
    parts = name.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


def route_query(message: str) -> str:
    msg_lower = message.lower()
    scores = {}
    for name, config in GENIE_SPACES.items():
        score = sum(1 for kw in config["keywords"] if kw in msg_lower)
        scores[name] = score
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    return list(GENIE_SPACES.keys())[0]


def init_session_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = None
    if "active_space" not in st.session_state:
        st.session_state.active_space = list(GENIE_SPACES.keys())[0]
    if "auto_route" not in st.session_state:
        st.session_state.auto_route = True
    if "active_tool" not in st.session_state:
        st.session_state.active_tool = None


def clear_conversation():
    st.session_state.messages = []
    st.session_state.conversation_id = None


def send_suggestion(text):
    st.session_state._pending_suggestion = text


def main():
    init_session_state()
    user_email = get_user_email()
    user_name = get_user_display_name(user_email)
    user_initials = get_user_initials(user_email)

    # Sidebar
    with st.sidebar:
        st.markdown(f"""
        <div style="padding: 4px 0 12px; display:flex; align-items:center; gap:10px;">
            <div style="width:26px; height:26px; border-radius:7px; background:linear-gradient(140deg, #8b3dff, #c026d3);"></div>
            <div style="font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:17px; letter-spacing:-0.01em; color:#1b1626 !important;">Argo</div>
        </div>
        """, unsafe_allow_html=True)

        if st.button("+ New query", use_container_width=True, type="primary"):
            clear_conversation()
            st.rerun()

        st.divider()

        # Genie spaces section
        st.markdown("""
        <div style="font-size:10.5px; font-weight:700; letter-spacing:0.13em; color:#9990a8; text-transform:uppercase; padding:0 0 8px;">
            Genie spaces
        </div>
        """, unsafe_allow_html=True)

        for name, config in GENIE_SPACES.items():
            color = config.get("color", "#a855f7")
            active = "font-weight:700;" if name == st.session_state.active_space else ""
            st.markdown(f"""
            <div class="space-item" style="{active}">
                <span class="space-dot" style="background:{color};"></span>
                {name}
            </div>
            """, unsafe_allow_html=True)

        st.divider()

        # Auto-route toggle
        st.session_state.auto_route = st.toggle(
            "Auto-route queries",
            value=st.session_state.auto_route,
            help="Automatically pick the best Genie space based on your question",
        )

        if not st.session_state.auto_route:
            selected_space = st.selectbox(
                "Genie Space",
                list(GENIE_SPACES.keys()),
                index=list(GENIE_SPACES.keys()).index(st.session_state.active_space),
                label_visibility="collapsed",
            )
            if selected_space != st.session_state.active_space:
                st.session_state.active_space = selected_space
                clear_conversation()
                st.rerun()

        # Tools section (permission-gated)
        if has_tool_access(user_email):
            st.divider()
            st.markdown("""
            <div style="font-size:10.5px; font-weight:700; letter-spacing:0.13em; color:#9990a8; text-transform:uppercase; padding:0 0 8px;">
                Tools
            </div>
            """, unsafe_allow_html=True)

            for tool_name, tool_config in TOOLS.items():
                is_active = st.session_state.active_tool == tool_name
                style = "font-weight:700; background:#ece7f7; border-radius:8px;" if is_active else ""
                if st.button(
                    f"{tool_config['icon']}  {tool_name}",
                    key=f"tool_{tool_name}",
                    use_container_width=True,
                ):
                    st.session_state.active_tool = tool_name
                    st.rerun()

            if st.session_state.active_tool:
                if st.button("← Back to Chat", key="back_to_chat", use_container_width=True):
                    st.session_state.active_tool = None
                    st.rerun()

        # Spacer + user profile at bottom
        st.markdown("<div style='flex:1;'></div>", unsafe_allow_html=True)
        st.divider()
        admin_badge = '  · <span style="color:#7c3aed; font-weight:600;">Admin</span>' if is_admin(user_email) else ''
        st.markdown(f"""
        <div style="display:flex; align-items:center; gap:11px; padding:4px 0;">
            <div style="width:34px; height:34px; border-radius:9px; background:linear-gradient(135deg,#7c3aed,#c026d3); display:flex; align-items:center; justify-content:center; font-weight:700; font-size:12px; color:#fff;">{user_initials}</div>
            <div style="line-height:1.3;">
                <div style="font-size:13px; font-weight:600; color:#1b1626 !important;">{user_name}</div>
                <div style="font-size:11px; color:#8a8199 !important;">{len(GENIE_SPACES)} spaces{admin_badge}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    # If a tool is active, render it instead of chat
    if st.session_state.active_tool and has_tool_access(user_email):
        tool_config = TOOLS.get(st.session_state.active_tool)
        if tool_config:
            tool_config["renderer"]()
        return

    # Handle pending suggestion (from welcome cards)
    pending = st.session_state.pop("_pending_suggestion", None)

    # Chat input is always visible
    prompt = pending or st.chat_input("Ask anything about your content data...")

    # Main content area
    if not st.session_state.messages and not prompt:
        _render_welcome(user_name)
    else:
        _render_chat(prompt, user_email)


def _render_welcome(user_name):
    """Render the welcome/landing screen with suggestions."""
    st.markdown(f"""
    <div style="padding: 60px 0 20px; text-align:left;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:32px; font-weight:700; letter-spacing:-0.02em; color:#1b1626; margin-bottom:8px;">
            Hello, {user_name}.
        </div>
        <div style="font-size:15px; color:#8a8199; margin-bottom:6px;">
            Ask a question and Argo routes it to the right Genie space.
        </div>
        <div class="status-indicator" style="margin-top:8px;">
            <span class="status-dot"></span>
            {len(GENIE_SPACES)} Genie spaces connected
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div style="font-size:11px; font-weight:700; letter-spacing:0.13em; color:#a49bb3; text-transform:uppercase; margin:24px 0 12px;">
        Try asking
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns(2)
    for idx, suggestion in enumerate(SUGGESTION_CARDS):
        with cols[idx % 2]:
            if st.button(suggestion, key=f"suggest_{idx}", use_container_width=True):
                send_suggestion(suggestion)
                st.rerun()


def _render_chat(prompt=None, user_email=None):
    """Render the chat conversation view."""
    # Status bar
    st.markdown(f"""
    <div style="display:flex; align-items:center; justify-content:flex-end; padding:0 0 12px; border-bottom:1px solid #eceaf2; margin-bottom:16px;">
        <div class="status-indicator">
            <span class="status-dot"></span>
            Auto-routing {'on' if st.session_state.auto_route else 'off'}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Render message history
    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"], avatar="🔮" if msg["role"] == "assistant" else None):
            # Routing badge for assistant messages
            if msg.get("routed_to") and msg["role"] == "assistant":
                space_name = msg["routed_to"]
                color = GENIE_SPACES.get(space_name, {}).get("color", "#a855f7")
                st.markdown(f"""
                <div class="routing-badge">
                    <span class="routing-dot" style="background:{color};"></span>
                    Answered by {space_name} <span style="color:#a49bb3; font-weight:400;">· routed automatically</span>
                </div>
                """, unsafe_allow_html=True)

            st.markdown(msg["content"])

            if msg.get("dataframe") is not None:
                df = pd.DataFrame(msg["dataframe"]["data"], columns=msg["dataframe"]["columns"])
                col_table, col_download = st.columns([6, 1])
                with col_table:
                    st.dataframe(df, use_container_width=True, hide_index=True)
                with col_download:
                    csv = df.to_csv(index=False)
                    st.download_button("Download CSV", csv, "query_result.csv", "text/csv", key=f"dl_{i}")

            if msg.get("sql"):
                with st.expander("View generated SQL"):
                    st.code(msg["sql"], language="sql")

            if msg.get("suggestions"):
                st.caption("**Suggested questions:**")
                for q in msg["suggestions"]:
                    st.caption(f"• {q}")

            # Feedback — all users get soft signal, admins also save benchmarks
            if msg["role"] == "assistant":
                admin = is_admin(user_email)
                feedback_key = f"feedback_{i}"
                feedback_reason_key = f"feedback_reason_{i}"
                if feedback_key not in st.session_state:
                    st.markdown('<div style="margin-top:10px;"><span style="font-size:12.5px; color:#8a8199;">Was this helpful?</span></div>', unsafe_allow_html=True)
                    col1, col2, col3 = st.columns([1, 1, 20])
                    with col1:
                        help_text = "Helpful — saves as benchmark" if admin else "Helpful"
                        if st.button("👍", key=f"up_{i}", help=help_text):
                            client = get_genie_client()
                            space_id = GENIE_SPACES[msg.get("_space", st.session_state.active_space)]["id"]
                            if msg.get("_message_id") and msg.get("_conversation_id"):
                                try:
                                    client.submit_feedback(space_id, msg["_conversation_id"], msg["_message_id"], "POSITIVE")
                                except Exception:
                                    pass
                            if admin and msg.get("sql") and i > 0:
                                user_question = st.session_state.messages[i - 1].get("content", "")
                                if user_question:
                                    try:
                                        client.save_benchmark(space_id, user_question, msg["sql"])
                                    except Exception:
                                        pass
                            st.session_state[feedback_key] = "positive"
                            st.rerun()
                    with col2:
                        if st.button("👎", key=f"down_{i}", help="Not helpful"):
                            if admin:
                                st.session_state[feedback_key] = "pending_reason"
                            else:
                                if msg.get("_message_id") and msg.get("_conversation_id"):
                                    client = get_genie_client()
                                    space_id = GENIE_SPACES[msg.get("_space", st.session_state.active_space)]["id"]
                                    try:
                                        client.submit_feedback(space_id, msg["_conversation_id"], msg["_message_id"], "NEGATIVE")
                                    except Exception:
                                        pass
                                st.session_state[feedback_key] = "negative"
                            st.rerun()
                elif st.session_state[feedback_key] == "pending_reason":
                    st.markdown("""
                    <div style="margin-top:8px; font-size:12.5px; font-weight:600; color:#b3323f;">What was wrong with this response?</div>
                    """, unsafe_allow_html=True)
                    reason = st.text_input(
                        "Reason",
                        key=f"reason_input_{i}",
                        placeholder="e.g. wrong genie, numbers look off, missing a column...",
                        label_visibility="collapsed",
                    )
                    col1, col2, _ = st.columns([1, 1, 10])
                    with col1:
                        if st.button("Send", key=f"submit_reason_{i}", type="primary"):
                            if msg.get("_message_id") and msg.get("_conversation_id"):
                                client = get_genie_client()
                                space_id = GENIE_SPACES[msg.get("_space", st.session_state.active_space)]["id"]
                                try:
                                    client.submit_feedback(space_id, msg["_conversation_id"], msg["_message_id"], "NEGATIVE")
                                except Exception:
                                    pass
                            st.session_state[feedback_key] = "negative"
                            st.session_state[feedback_reason_key] = reason or ""
                            st.rerun()
                    with col2:
                        if st.button("Skip", key=f"skip_reason_{i}"):
                            if msg.get("_message_id") and msg.get("_conversation_id"):
                                client = get_genie_client()
                                space_id = GENIE_SPACES[msg.get("_space", st.session_state.active_space)]["id"]
                                try:
                                    client.submit_feedback(space_id, msg["_conversation_id"], msg["_message_id"], "NEGATIVE")
                                except Exception:
                                    pass
                            st.session_state[feedback_key] = "negative"
                            st.session_state[feedback_reason_key] = ""
                            st.rerun()
                else:
                    feedback = st.session_state[feedback_key]
                    reason = st.session_state.get(feedback_reason_key, "")
                    if feedback == "positive":
                        has_sql = admin and msg.get("sql") and i > 0
                        label = "✓ Saved as benchmark" if has_sql else "✓ Thanks for the feedback"
                        st.markdown(f'<div style="font-size:12.5px; color:#0d9488; margin-top:8px;">{label}</div>', unsafe_allow_html=True)
                    else:
                        label = "✓ Feedback sent"
                        if reason:
                            label += f" — {reason}"
                        st.markdown(f'<div style="font-size:12.5px; color:#0d9488; margin-top:8px;">{label}</div>', unsafe_allow_html=True)

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})

        with st.chat_message("user"):
            st.markdown(prompt)

        if st.session_state.auto_route:
            routed_space = route_query(prompt)
            if routed_space != st.session_state.active_space:
                st.session_state.active_space = routed_space
                st.session_state.conversation_id = None
        else:
            routed_space = st.session_state.active_space

        with st.chat_message("assistant", avatar="🔮"):
            # Show routing badge
            color = GENIE_SPACES[routed_space].get("color", "#a855f7")
            st.markdown(f"""
            <div class="routing-badge">
                <span class="routing-dot" style="background:{color};"></span>
                Routing to {routed_space}...
            </div>
            """, unsafe_allow_html=True)

            progress = st.empty()
            progress.caption("⏳ Sending question...")
            client = get_genie_client()
            space_id = GENIE_SPACES[routed_space]["id"]

            def update_status(msg):
                progress.caption(f"⏳ {msg}")

            try:
                response = client.ask(
                    space_id=space_id,
                    message=prompt,
                    conversation_id=st.session_state.conversation_id,
                    on_status=update_status,
                )

                parsed = client.parse_response(response)
                st.session_state.conversation_id = parsed["conversation_id"]
                progress.empty()

                if parsed["status"] == "COMPLETED":
                    answer = parsed["text"] or parsed.get("query_description") or ""
                    query_df = None
                    df_data = None
                    msg_id = response.get("_message_id") or response.get("id") or ""
                    att_id = parsed.get("_query_attachment_id")

                    if att_id and msg_id:
                        try:
                            import time as _time
                            update_status("Fetching results...")
                            qr = None
                            for attempt in range(3):
                                qr = client.get_query_result(
                                    space_id=space_id,
                                    conversation_id=parsed["conversation_id"],
                                    message_id=msg_id,
                                    attachment_id=att_id,
                                )
                                stmt = qr.get("statement_response", {})
                                if stmt.get("status", {}).get("state") == "SUCCEEDED" or stmt.get("result") or stmt.get("manifest"):
                                    break
                                _time.sleep(2)
                            progress.empty()

                            stmt = qr.get("statement_response", {}) if qr else {}
                            columns = [col["name"] for col in stmt.get("manifest", {}).get("schema", {}).get("columns", [])]
                            rows = []
                            for chunk in stmt.get("result", {}).get("data_typed_array", []):
                                row = [v.get("str", v.get("value", "")) for v in chunk.get("values", [])]
                                rows.append(row)
                            if not rows:
                                for chunk in stmt.get("result", {}).get("data_array", []):
                                    rows.append(chunk)
                            if not rows and not columns:
                                columns = [col["name"] for col in qr.get("manifest", {}).get("schema", {}).get("columns", [])]
                                for chunk in qr.get("result", {}).get("data_array", []):
                                    rows.append(chunk)
                            if columns and rows:
                                query_df = pd.DataFrame(rows, columns=columns)
                                df_data = {"columns": columns, "data": rows}
                        except Exception as e:
                            progress.empty()
                            st.warning(f"Could not fetch query results: {e}")

                    if answer:
                        st.markdown(answer)
                    if query_df is not None:
                        col_table, col_download = st.columns([6, 1])
                        with col_table:
                            st.dataframe(query_df, use_container_width=True, hide_index=True)
                        with col_download:
                            csv = query_df.to_csv(index=False)
                            st.download_button("Download CSV", csv, "query_result.csv", "text/csv", key="dl_live")
                    elif parsed.get("sql") and not answer:
                        st.info("Query ran but no results were returned.")

                    display_text = answer or ("Query returned results" if query_df is not None else "No results")
                    assistant_msg = {
                        "role": "assistant",
                        "content": display_text,
                        "_message_id": msg_id,
                        "_conversation_id": parsed["conversation_id"],
                        "_space": routed_space,
                        "routed_to": routed_space,
                    }
                    if df_data:
                        assistant_msg["dataframe"] = df_data

                    if parsed["sql"]:
                        with st.expander("View generated SQL"):
                            st.code(parsed["sql"], language="sql")
                        assistant_msg["sql"] = parsed["sql"]

                    if parsed.get("error"):
                        with st.expander("Query Warning"):
                            st.caption(parsed["error"])

                    if parsed["suggested_questions"]:
                        st.caption("**Suggested questions:**")
                        for q in parsed["suggested_questions"]:
                            st.caption(f"• {q}")
                        assistant_msg["suggestions"] = parsed["suggested_questions"]

                elif parsed["status"] == "FAILED":
                    error_detail = parsed.get("error") or "No additional detail available."
                    error_text = f"The query failed: {error_detail}"
                    st.error(error_text)
                    if parsed.get("text"):
                        st.info(parsed["text"])
                    assistant_msg = {"role": "assistant", "content": error_text}

                else:
                    last_seen = parsed.get("raw_status") or "unknown"
                    timeout_text = f"The query timed out after 3 minutes (last state: {last_seen}). Try again in a moment or rephrase your question."
                    st.warning(timeout_text)
                    assistant_msg = {"role": "assistant", "content": timeout_text}

            except Exception as e:
                progress.empty()
                error_text = f"Error communicating with Genie: {str(e)}"
                st.error(error_text)
                assistant_msg = {"role": "assistant", "content": error_text}

            st.session_state.messages.append(assistant_msg)
            st.rerun()


if __name__ == "__main__":
    main()
