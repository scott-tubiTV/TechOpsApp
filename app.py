"""Argo — TechOps content metrics chat interface powered by Databricks Genie."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st
from genie_client import GenieClient
from llm_client import LLMClient
from query_decomposer import QueryDecomposer, DecompositionResult, SubQuery
from result_synthesizer import ResultSynthesizer, SpaceResult
from conversation_store import (
    ensure_table_exists,
    save_conversation,
    list_conversations,
    load_conversation,
    share_conversation,
    archive_conversation,
)
from tools.sony_matcher import render_content_id_matcher
from tools.imdb_dupe_checker import render_imdb_dupe_checker
from tools.genie_benchmark import render_genie_benchmark
from saved_queries_tab import render_saved_queries_tab

ADMIN_EMAILS = [
    "swhitney@tubi.tv",
]

TOOL_USERS = [
    "swhitney@tubi.tv",
    "caitlinlittle@tubi.tv",
    "marianladiona@tubi.tv",
    "jhudson@tubi.tv",
    "james@tubi.tv",
    "mmaynez@tubi.tv",
    "hfong@tubi.tv",
    "lgumawid@tubi.tv",
    "rramirez@tubi.tv",
    "aandrewsprince@tubi.tv",
]

TOOLS = {
    "Content ID Matcher": {
        "icon": "🔍",
        "description": "Look up Tubi content IDs by title from the live database",
        "renderer": render_content_id_matcher,
    },
    "IMDB Dupe Checker": {
        "icon": "🎬",
        "description": "Find IMDB IDs, detect duplicates, and check policy conflicts",
        "renderer": render_imdb_dupe_checker,
    },
    "Reports": {
        "icon": "📊",
        "description": "Saved queries and dashboard metrics you can refresh on demand",
        "renderer": render_saved_queries_tab,
    },
}

GENIE_SPACES = {
    "Content Metrics": {
        "id": os.environ.get("GENIE_SPACE_TECHOPS", "01f18b97de0117d8af61d23c1eaa8d7e"),
        "description": "Ops metrics: policy snapshots, avails status, imports, images, partners, QA/ABF, workload, content refresh",
        "keywords": ["policy", "avail", "import", "image", "qa", "abf", "workload", "snapshot", "metric", "weekly", "monthly", "refresh", "gate", "published", "curation", "how many", "count", "total", "trend", "queue", "review"],
        "color": "#a855f7",
    },
    "Redeliveries": {
        "id": os.environ.get("GENIE_SPACE_REDELIVERIES", "01f18b97de2f122eb27a1b307d68e50e"),
        "description": "Redelivery detail: per-title breakdown by reason, modality, age, partner, and dismissal status",
        "keywords": ["redelivery", "redeliver", "dismissed", "modality", "video file", "subtitle file", "image file", "overdue", "backlog", "oldest", "reason", "open redeliveries"],
        "color": "#f59e0b",
    },
    "Dupe Checker V2": {
        "id": os.environ.get("GENIE_SPACE_DUPE_CHECKER", "01f122f4e2921b7a9c28ef03d0812ee6"),
        "description": "Duplicate title detection: upload CSV avails to check for existing titles and conflicts",
        "keywords": ["dupe", "duplicate", "conflict", "csv", "upload", "check", "avails file", "overlap", "territory"],
        "color": "#f472b6",
    },
    "Partner Detail": {
        "id": os.environ.get("GENIE_SPACE_PARTNER_DETAIL", "01f1912d11a91341b59b2836ca3777c6"),
        "description": "Per-partner health: error rates, redeliveries, pipeline failures, ABF errors, POC info",
        "keywords": ["partner", "error rate", "sony", "paramount", "nbcu", "lionsgate", "endemol", "all3", "shout", "redelivery reason", "pipeline failure", "abf error", "poc", "priority partner", "inactive", "health", "which partners", "partner error", "partner issue", "partner redeliveries", "overdue"],
        "color": "#10b981",
    },
    "Content Library": {
        "id": os.environ.get("GENIE_SPACE_CONTENT_LIBRARY", "01f1951ff07e199aa6d9c0718f342e35"),
        "description": "Per-title metadata: look up individual titles by name, partner, content type, internal tags, live status, policy windows",
        "keywords": ["title", "movie", "series", "episode", "creator_content", "same_day", "tubi_original", "live", "is live", "policy window", "genre", "imdb", "release year", "which titles", "find title", "look up", "content_id", "asset status", "has video", "has subtitles"],
        "color": "#06b6d4",
    },
}

SUGGESTION_CARDS = [
    "How many new avails were created this month by content type?",
    "How many open redeliveries are there by asset type?",
    "Which partners have the highest error rate?",
    "How many titles are in the ABF review queue right now?",
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


@st.cache_resource
def get_llm_client():
    return LLMClient()


@st.cache_resource
def get_decomposer():
    return QueryDecomposer(get_llm_client())


@st.cache_resource
def get_synthesizer():
    return ResultSynthesizer(get_llm_client())


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


def decompose_query(prompt: str) -> DecompositionResult:
    """Use LLM decomposer with keyword fallback."""
    try:
        return get_decomposer().decompose(prompt)
    except Exception:
        fallback_space = route_query(prompt)
        return DecompositionResult(
            is_multi=False,
            sub_queries=[SubQuery(space_name=fallback_space, sub_query=prompt)],
            reasoning="Fallback to keyword routing",
        )


def _query_single_space(sub_query: SubQuery, conversation_id: str = None) -> SpaceResult:
    """Execute a single Genie query and return a SpaceResult."""
    client = get_genie_client()
    space_id = GENIE_SPACES[sub_query.space_name]["id"]
    try:
        response = client.ask(space_id=space_id, message=sub_query.sub_query, conversation_id=conversation_id)
        parsed = client.parse_response(response)

        text = parsed.get("text") or parsed.get("query_description") or ""
        sql = parsed.get("sql")
        columns = []
        rows = []

        msg_id = response.get("_message_id") or response.get("id") or ""
        att_id = parsed.get("_query_attachment_id")
        if att_id and msg_id and parsed["status"] == "COMPLETED":
            import time as _time
            qr = None
            for _ in range(3):
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

            stmt = qr.get("statement_response", {}) if qr else {}
            columns = [col["name"] for col in stmt.get("manifest", {}).get("schema", {}).get("columns", [])]
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

        status = "COMPLETED" if parsed["status"] == "COMPLETED" else "FAILED"
        return SpaceResult(
            space_name=sub_query.space_name,
            sub_query=sub_query.sub_query,
            status=status,
            text=text,
            sql=sql,
            columns=columns,
            rows=rows,
            error=parsed.get("error"),
        )
    except Exception as e:
        return SpaceResult(
            space_name=sub_query.space_name,
            sub_query=sub_query.sub_query,
            status="FAILED",
            error=str(e),
        )


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
    if "argo_conversation_id" not in st.session_state:
        st.session_state.argo_conversation_id = None


@st.cache_resource
def _ensure_persistence_ready():
    try:
        ensure_table_exists()
        return True
    except Exception as e:
        st.warning(f"Conversation persistence unavailable: {e}")
        return False


def clear_conversation():
    st.session_state.messages = []
    st.session_state.conversation_id = None
    st.session_state.argo_conversation_id = None


def send_suggestion(text):
    st.session_state._pending_suggestion = text


def _load_past_conversation(conversation_id, user_email):
    conv = load_conversation(conversation_id, requesting_email=user_email)
    if conv is None:
        return
    st.session_state.messages = conv["messages"]
    st.session_state.argo_conversation_id = conv["conversation_id"]
    st.session_state.active_space = conv.get("space_name") or list(GENIE_SPACES.keys())[0]
    st.session_state.conversation_id = None
    st.session_state.active_tool = None


def main():
    init_session_state()
    _ensure_persistence_ready()
    user_email = get_user_email()
    user_name = get_user_display_name(user_email)
    user_initials = get_user_initials(user_email)

    # Sidebar
    with st.sidebar:
        st.markdown(f"""
        <div style="padding: 4px 0 12px; display:flex; align-items:center; gap:10px;">
            <svg width="28" height="28" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
                <rect width="28" height="28" rx="7" fill="url(#argo_grad)"/>
                <path d="M14 6L16.5 11.5L22 14L16.5 16.5L14 22L11.5 16.5L6 14L11.5 11.5L14 6Z" fill="white" opacity="0.95"/>
                <path d="M14 9L15.5 12.5L19 14L15.5 15.5L14 19L12.5 15.5L9 14L12.5 12.5L14 9Z" fill="white"/>
                <defs><linearGradient id="argo_grad" x1="0" y1="0" x2="28" y2="28"><stop stop-color="#7c3aed"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
            </svg>
            <div style="font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:17px; letter-spacing:-0.01em; color:#1b1626 !important;">Argo</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <style>
        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: transparent !important;
            color: #7c3aed !important;
            border: 1.5px solid #7c3aed !important;
            box-shadow: 0 2px 8px rgba(124,58,237,0.12);
            font-weight: 700;
        }
        [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
            background: #f3ecfd !important;
            box-shadow: 0 4px 14px rgba(124,58,237,0.18);
        }
        </style>
        """, unsafe_allow_html=True)

        if st.button("+ New query", use_container_width=True, type="primary"):
            clear_conversation()
            st.rerun()

        st.divider()

        # Conversation history
        if "show_history_panel" not in st.session_state:
            st.session_state.show_history_panel = False

        st.markdown("""
        <div style="font-size:10.5px; font-weight:700; letter-spacing:0.13em; color:#9990a8; text-transform:uppercase; padding:0 0 6px;">
            Recent
        </div>
        """, unsafe_allow_html=True)

        try:
            recent_conversations = list_conversations(user_email, search="", limit=5)
        except Exception:
            recent_conversations = []

        if recent_conversations:
            for conv in recent_conversations:
                is_current = (st.session_state.argo_conversation_id == conv["conversation_id"])
                label = conv["title"] or "Untitled"
                if conv.get("shared_with") and conv.get("user_email") != user_email:
                    label = f"[Shared] {label}"
                btn_type = "primary" if is_current else "secondary"
                if st.button(
                    label,
                    key=f"conv_{conv['conversation_id']}",
                    use_container_width=True,
                    type=btn_type,
                ):
                    _load_past_conversation(conv["conversation_id"], user_email)
                    st.rerun()
        elif not st.session_state.show_history_panel:
            st.caption("No conversations yet.")

        if st.button("View all conversations", key="view_all_convos", use_container_width=True):
            st.session_state.show_history_panel = not st.session_state.show_history_panel
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

    # Admin: Genie instructions manager
    if is_admin(user_email):
        with st.sidebar:
            if st.button("⚙ Manage Instructions", key="manage_instructions", use_container_width=True):
                st.session_state.active_tool = "_instructions"
                st.rerun()
            if st.button("📊 Genie Benchmark", key="genie_benchmark", use_container_width=True):
                st.session_state.active_tool = "_benchmark"
                st.rerun()

    # If a tool is active, render it instead of chat
    if st.session_state.active_tool == "_instructions" and is_admin(user_email):
        _render_instructions_manager()
        return
    if st.session_state.active_tool == "_benchmark" and is_admin(user_email):
        render_genie_benchmark()
        return
    if st.session_state.active_tool and has_tool_access(user_email):
        tool_config = TOOLS.get(st.session_state.active_tool)
        if tool_config:
            tool_config["renderer"]()
        return

    # Full conversation history panel
    if st.session_state.get("show_history_panel"):
        _render_history_panel(user_email)
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


def _render_history_panel(user_email):
    """Full conversation history panel with search and date grouping."""
    if st.button("← Back to Chat", key="back_from_history"):
        st.session_state.show_history_panel = False
        st.rerun()

    st.markdown("""
    <div style="padding: 12px 0 4px;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.02em; color:#1b1626;">
            Conversation History
        </div>
        <div style="font-size:13px; color:#8a8199; margin-top:4px;">
            Search or browse all past conversations.
        </div>
    </div>
    """, unsafe_allow_html=True)

    history_search = st.text_input(
        "Search conversations",
        key="history_search",
        placeholder="Search by topic...",
        label_visibility="collapsed",
    )

    try:
        all_conversations = list_conversations(user_email, search=history_search or "", limit=50)
    except Exception:
        all_conversations = []

    if not all_conversations:
        st.info("No conversations found." if history_search else "No conversations yet.")
        return

    from datetime import datetime, date

    today = date.today()
    grouped = {}
    for conv in all_conversations:
        updated = conv.get("updated_at", "")
        try:
            conv_date = datetime.fromisoformat(updated.replace("Z", "+00:00")).date()
            delta = (today - conv_date).days
            if delta == 0:
                group = "Today"
            elif delta == 1:
                group = "Yesterday"
            elif delta < 7:
                group = "This week"
            elif delta < 30:
                group = "This month"
            else:
                group = conv_date.strftime("%B %Y")
        except (ValueError, AttributeError):
            group = "Older"
        grouped.setdefault(group, []).append(conv)

    for group_name, convs in grouped.items():
        st.markdown(f"""
        <div style="font-size:11px; font-weight:700; letter-spacing:0.1em; color:#9990a8; text-transform:uppercase; padding:16px 0 6px; border-bottom:1px solid #e7e3ee; margin-bottom:4px;">
            {group_name}
        </div>
        """, unsafe_allow_html=True)

        for conv in convs:
            is_current = (st.session_state.argo_conversation_id == conv["conversation_id"])
            title = conv.get("title") or "Untitled"
            space = conv.get("space_name") or ""
            msg_count = conv.get("message_count", 0)
            shared = "[Shared] " if conv.get("shared_with") and conv.get("user_email") != user_email else ""

            col_title, col_meta = st.columns([4, 1])
            with col_title:
                btn_type = "primary" if is_current else "secondary"
                if st.button(
                    f"{shared}{title}",
                    key=f"hist_{conv['conversation_id']}",
                    use_container_width=True,
                    type=btn_type,
                ):
                    _load_past_conversation(conv["conversation_id"], user_email)
                    st.session_state.show_history_panel = False
                    st.rerun()
            with col_meta:
                st.caption(f"{space}  ·  {msg_count} msgs")


def _render_instructions_manager():
    """Admin tool: push instructions to Genie spaces via the SP's credentials."""
    st.markdown("### Genie Instructions Manager")
    st.caption("Push instructions directly to Genie spaces using the app's service principal.")

    client = get_genie_client()

    space_name = st.selectbox("Space", list(GENIE_SPACES.keys()))
    space_id = GENIE_SPACES[space_name]["id"]
    st.code(f"Space ID: {space_id}", language=None)

    current = ""
    try:
        space_data = client.get_space(space_id)
        serialized = space_data.get("serialized_space", {})
        if isinstance(serialized, str):
            import json as _json
            serialized = _json.loads(serialized) if serialized else {}
        current = serialized.get("instructions", {}).get("text_instructions", "")
    except Exception as e:
        st.warning(f"Could not read current instructions: {e}")

    instructions = st.text_area(
        "Instructions",
        value=current,
        height=400,
        placeholder="Enter instructions for this Genie space...",
    )

    if st.button("Push Instructions", type="primary"):
        try:
            result = client.set_instructions(space_id, instructions)
            st.success("Instructions pushed successfully!")
            serialized_back = result.get("serialized_space", {})
            if isinstance(serialized_back, str):
                import json as _json
                serialized_back = _json.loads(serialized_back) if serialized_back else {}
            saved = serialized_back.get("instructions", {}).get("text_instructions", "")
            if saved:
                st.caption(f"Confirmed: {len(saved)} chars saved")
            else:
                st.caption("Note: response didn't confirm instructions (may still have worked)")
        except Exception as e:
            st.error(f"Failed: {e}")

    if st.button("← Back to Chat", key="back_from_instructions"):
        st.session_state.active_tool = None
        st.rerun()


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
    # Status bar with share button
    bar_col1, bar_col2 = st.columns([8, 1])
    with bar_col1:
        st.markdown(f"""
        <div style="display:flex; align-items:center; padding:0 0 12px; border-bottom:1px solid #eceaf2; margin-bottom:16px;">
            <div class="status-indicator">
                <span class="status-dot"></span>
                Auto-routing {'on' if st.session_state.auto_route else 'off'}
            </div>
        </div>
        """, unsafe_allow_html=True)
    with bar_col2:
        if st.session_state.argo_conversation_id and not is_admin(user_email):
            if st.button("Share", key="share_admin", help="Share this conversation with an admin for debugging"):
                try:
                    for admin_email in ADMIN_EMAILS:
                        share_conversation(st.session_state.argo_conversation_id, admin_email)
                    st.toast("Shared with admin!")
                except Exception:
                    st.toast("Failed to share", icon="⚠️")

    # Render message history
    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"], avatar="🔮" if msg["role"] == "assistant" else None):
            # Routing badge for assistant messages
            if msg.get("routed_to") and msg["role"] == "assistant":
                routed_to = msg["routed_to"]
                if " + " in routed_to:
                    space_names = [s.strip() for s in routed_to.split(" + ")]
                    dots = ""
                    for sn in space_names:
                        c = GENIE_SPACES.get(sn, {}).get("color", "#a855f7")
                        dots += f'<span class="routing-dot" style="background:{c};"></span> {sn}  '
                    st.markdown(f"""
                    <div class="routing-badge">
                        {dots}
                        <span style="color:#a49bb3; font-weight:400;">· multi-space query</span>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    color = GENIE_SPACES.get(routed_to, {}).get("color", "#a855f7")
                    st.markdown(f"""
                    <div class="routing-badge">
                        <span class="routing-dot" style="background:{color};"></span>
                        Answered by {routed_to} <span style="color:#a49bb3; font-weight:400;">· routed automatically</span>
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

            if msg.get("sql") and msg.get("dataframe") is not None and has_tool_access(user_email):
                save_key = f"save_report_{i}"
                if st.session_state.get(save_key) == "form":
                    with st.form(key=f"save_report_form_{i}"):
                        sr_title = st.text_input("Report title", value=st.session_state.messages[i - 1]["content"][:60] if i > 0 else "")
                        sr_desc = st.text_input("Description (optional)")
                        sr_display = st.selectbox("Display type", ["table", "metric", "bar_chart", "line_chart", "pie_chart"])
                        sr_col1, sr_col2 = st.columns(2)
                        with sr_col1:
                            sr_submitted = st.form_submit_button("Save", use_container_width=True)
                        with sr_col2:
                            sr_cancelled = st.form_submit_button("Cancel", use_container_width=True)
                        if sr_submitted and sr_title:
                            from saved_queries_store import save_query as _save_q
                            prompt_text = st.session_state.messages[i - 1]["content"] if i > 0 else None
                            _save_q(
                                user_email=user_email,
                                title=sr_title,
                                sql_text=msg["sql"],
                                display_type=sr_display,
                                description=sr_desc,
                                original_prompt=prompt_text,
                                genie_space=msg.get("routed_to") or msg.get("_space"),
                            )
                            st.session_state[save_key] = "saved"
                            st.rerun()
                        if sr_cancelled:
                            st.session_state.pop(save_key, None)
                            st.rerun()
                elif st.session_state.get(save_key) == "saved":
                    st.caption("Saved to Reports.")
                else:
                    if st.button("📊 Save as Report", key=f"save_btn_{i}"):
                        st.session_state[save_key] = "form"
                        st.rerun()

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
            decomposition = decompose_query(prompt)
        else:
            decomposition = DecompositionResult(
                is_multi=False,
                sub_queries=[SubQuery(space_name=st.session_state.active_space, sub_query=prompt)],
                reasoning="Manual space selection",
            )

        if decomposition.is_multi:
            # --- Multi-space query path ---
            with st.chat_message("assistant", avatar="🔮"):
                space_badges = ""
                for sq in decomposition.sub_queries:
                    color = GENIE_SPACES.get(sq.space_name, {}).get("color", "#a855f7")
                    space_badges += f'<span class="routing-dot" style="background:{color};"></span> {sq.space_name}  '
                st.markdown(f"""
                <div class="routing-badge">
                    {space_badges}
                    <span style="color:#a49bb3; font-weight:400;">· multi-space query</span>
                </div>
                """, unsafe_allow_html=True)

                progress_slots = {}
                for sq in decomposition.sub_queries:
                    progress_slots[sq.space_name] = st.empty()
                    progress_slots[sq.space_name].caption(f"⏳ Querying {sq.space_name}...")

                space_results = []
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {
                        executor.submit(_query_single_space, sq): sq
                        for sq in decomposition.sub_queries
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        space_results.append(result)
                        icon = "✅" if result.status == "COMPLETED" else "❌"
                        progress_slots[result.space_name].caption(f"{icon} {result.space_name}")

                completed = [r for r in space_results if r.status == "COMPLETED"]
                if completed:
                    synthesizer = get_synthesizer()
                    synthesis = synthesizer.synthesize(prompt, space_results)
                    st.markdown(synthesis)
                else:
                    synthesis = "All space queries failed. Please try rephrasing your question."
                    st.error(synthesis)

                for result in space_results:
                    if result.status == "COMPLETED" and result.columns and result.rows:
                        with st.expander(f"Data from {result.space_name}"):
                            df = pd.DataFrame(result.rows, columns=result.columns)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                            if result.sql:
                                st.code(result.sql, language="sql")

                routed_label = " + ".join(sq.space_name for sq in decomposition.sub_queries)
                assistant_msg = {
                    "role": "assistant",
                    "content": synthesis,
                    "routed_to": routed_label,
                    "_multi_space": True,
                }
                first_completed = next((r for r in space_results if r.status == "COMPLETED" and r.columns and r.rows), None)
                if first_completed:
                    assistant_msg["dataframe"] = {"columns": first_completed.columns, "data": first_completed.rows}
                first_sql = next((r.sql for r in space_results if r.sql), None)
                if first_sql:
                    assistant_msg["sql"] = first_sql

                st.session_state.messages.append(assistant_msg)
                st.session_state.active_space = decomposition.sub_queries[0].space_name
                st.session_state.conversation_id = None

                try:
                    argo_conv_id = save_conversation(
                        user_email=user_email,
                        messages=st.session_state.messages,
                        space_name=routed_label,
                        conversation_id=st.session_state.argo_conversation_id,
                    )
                    st.session_state.argo_conversation_id = argo_conv_id
                except Exception as e:
                    st.toast(f"Save failed: {e}", icon="⚠️")

                st.rerun()

        else:
            # --- Single-space query path ---
            routed_space = decomposition.sub_queries[0].space_name
            if routed_space != st.session_state.active_space:
                st.session_state.active_space = routed_space
                st.session_state.conversation_id = None

            with st.chat_message("assistant", avatar="🔮"):
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

                try:
                    argo_conv_id = save_conversation(
                        user_email=user_email,
                        messages=st.session_state.messages,
                        space_name=st.session_state.active_space,
                        conversation_id=st.session_state.argo_conversation_id,
                    )
                    st.session_state.argo_conversation_id = argo_conv_id
                except Exception as e:
                    st.toast(f"Save failed: {e}", icon="⚠️")

                st.rerun()


if __name__ == "__main__":
    main()
