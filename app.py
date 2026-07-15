"""Content Ops Genie — Chat interface for content org data queries."""

import os
import pandas as pd
import streamlit as st
from genie_client import GenieClient

GENIE_SPACES = {
    "Content Metrics": {
        "id": os.environ.get("GENIE_SPACE_TECHOPS", "01f1808d550e12fb9bd0578798518174"),
        "description": "Ops metrics: policy snapshots, avails status, imports, images, partners, QA/ABF, redeliveries, workload",
        "keywords": ["policy", "avail", "import", "image", "partner", "qa", "abf", "redelivery", "workload", "snapshot", "metric", "weekly", "monthly"],
    },
    "Dupe Checker V2": {
        "id": os.environ.get("GENIE_SPACE_DUPE_CHECKER", "01f122f4e2921b7a9c28ef03d0812ee6"),
        "description": "Duplicate title detection: upload CSV avails to check for existing titles and conflicts",
        "keywords": ["dupe", "duplicate", "conflict", "csv", "upload", "check", "avails file", "overlap", "territory"],
    },
}

st.set_page_config(
    page_title="Content Ops Genie | TechOps",
    page_icon="🔮",
    layout="wide",
)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    .stApp header {background-color: #1a0a2e;}
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1a0a2e 0%, #2d1b4e 100%);
        border-right: 1px solid rgba(139, 92, 246, 0.15);
    }
    [data-testid="stChatMessage"] {
        border-radius: 12px;
        margin-bottom: 10px;
        border: 1px solid rgba(139, 92, 246, 0.1);
    }
    .stChatInput > div {border-color: rgba(139, 92, 246, 0.4) !important;}
    .stChatInput > div:focus-within {border-color: #d4a843 !important; box-shadow: 0 0 12px rgba(212, 168, 67, 0.15);}
    .stDataFrame {border-radius: 12px; border: 1px solid rgba(139, 92, 246, 0.15);}
    div[data-testid="stExpander"] {border-color: rgba(139, 92, 246, 0.2);}
    .stButton > button {
        border: 1px solid rgba(139, 92, 246, 0.3);
        border-radius: 8px;
        transition: all 0.3s ease;
    }
    .stButton > button:hover {
        border-color: #d4a843;
        box-shadow: 0 0 15px rgba(212, 168, 67, 0.1);
    }
    .stSelectbox > div > div {border-color: rgba(139, 92, 246, 0.2) !important;}
    h1, h2, h3 {font-family: 'Inter', sans-serif !important;}
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_genie_client():
    return GenieClient()


def get_user_email():
    """Get the authenticated user's email from Streamlit headers."""
    headers = st.context.headers
    email = headers.get("X-Forwarded-Email") or headers.get("X-Forwarded-Preferred-Username")
    if email:
        return email
    try:
        client = get_genie_client()
        return client.w.current_user.me().user_name
    except Exception:
        return "unknown"


def route_query(message: str) -> str:
    """Rule-based router: pick the best Genie space for a given message."""
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
        st.session_state.auto_route = False


def clear_conversation():
    st.session_state.messages = []
    st.session_state.conversation_id = None


def main():
    init_session_state()

    with st.sidebar:
        st.markdown("""
        <div style="text-align:center; padding: 0.5rem 0 1rem;">
            <div style="display:inline-flex; align-items:center; justify-content:center; width:42px; height:42px; background:linear-gradient(135deg, #8b5cf6, #d4a843); border-radius:10px; font-weight:800; font-size:0.9rem; color:#fff; box-shadow: 0 0 20px rgba(139,92,246,0.3); margin-bottom:0.5rem;">TO</div>
            <div style="font-size:1.3rem; font-weight:700; background:linear-gradient(135deg, #f0c75e, #d4a843); -webkit-background-clip:text; -webkit-text-fill-color:transparent;">Content Ops Genie</div>
            <div style="font-size:0.7rem; color:#9b8bb8; letter-spacing:2px; text-transform:uppercase; margin-top:0.25rem;">Technical Operations</div>
        </div>
        """, unsafe_allow_html=True)
        st.divider()
        user_email = get_user_email()
        st.caption(f"Logged in as: **{user_email}**")

        st.divider()

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
            )
            if selected_space != st.session_state.active_space:
                st.session_state.active_space = selected_space
                clear_conversation()
                st.rerun()

        st.divider()

        if st.button("New Conversation", use_container_width=True):
            clear_conversation()
            st.rerun()

        st.divider()
        st.caption("**Available Spaces:**")
        for name, config in GENIE_SPACES.items():
            icon = "✅" if name == st.session_state.active_space else "○"
            st.caption(f"{icon} **{name}**")
            st.caption(f"   {config['description']}")

    if st.session_state.auto_route:
        st.header("💬 Content Ops Genie (Auto-routing)")
    else:
        st.header(f"💬 {st.session_state.active_space}")

    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"], avatar="🔮" if msg["role"] == "assistant" else None):
            st.markdown(msg["content"])
            if msg.get("routed_to"):
                st.caption(f"🔀 Routed to: **{msg['routed_to']}**")
            if msg.get("sql"):
                with st.expander("SQL Query"):
                    st.code(msg["sql"], language="sql")
            if msg.get("suggestions"):
                st.caption("**Suggested questions:**")
                for q in msg["suggestions"]:
                    st.caption(f"• {q}")
            if msg.get("_message_id") and msg["role"] == "assistant":
                feedback_key = f"feedback_{i}"
                feedback_reason_key = f"feedback_reason_{i}"
                if feedback_key not in st.session_state:
                    col1, col2, col3 = st.columns([1, 1, 20])
                    with col1:
                        if st.button("👍", key=f"up_{i}", help="Helpful"):
                            client = get_genie_client()
                            space_id = GENIE_SPACES[msg.get("_space", st.session_state.active_space)]["id"]
                            try:
                                client.submit_feedback(space_id, msg["_conversation_id"], msg["_message_id"], "POSITIVE")
                            except Exception:
                                pass
                            st.session_state[feedback_key] = "positive"
                            st.rerun()
                    with col2:
                        if st.button("👎", key=f"down_{i}", help="Not helpful"):
                            st.session_state[feedback_key] = "pending_reason"
                            st.rerun()
                elif st.session_state[feedback_key] == "pending_reason":
                    st.caption("👎 What was wrong with this response?")
                    reason = st.text_input(
                        "Reason (optional)",
                        key=f"reason_input_{i}",
                        placeholder="e.g. Wrong numbers, used wrong table, too slow...",
                        label_visibility="collapsed",
                    )
                    col1, col2, _ = st.columns([1, 1, 10])
                    with col1:
                        if st.button("Submit", key=f"submit_reason_{i}"):
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
                        st.caption("👍 Thanks for the feedback!")
                    else:
                        label = "👎 Feedback submitted"
                        if reason:
                            label += f" — *{reason}*"
                        st.caption(label)

    if prompt := st.chat_input("Ask a question about content data..."):
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
                    msg_id = response.get("_message_id") or response.get("id", "")
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
                        except Exception as e:
                            progress.empty()
                            st.warning(f"Could not fetch query results: {e}")

                    if answer:
                        st.markdown(answer)
                    if query_df is not None:
                        st.dataframe(query_df, use_container_width=True)
                    elif parsed.get("sql") and not answer:
                        st.info("Query ran but no results were returned.")

                    display_text = answer or ("Query returned results" if query_df is not None else "No results")
                    assistant_msg = {
                        "role": "assistant",
                        "content": display_text,
                        "_message_id": msg_id,
                        "_conversation_id": parsed["conversation_id"],
                        "_space": routed_space,
                    }

                    if st.session_state.auto_route:
                        st.caption(f"🔀 Routed to: **{routed_space}**")
                        assistant_msg["routed_to"] = routed_space

                    if parsed["sql"]:
                        with st.expander("SQL Query"):
                            st.code(parsed["sql"], language="sql")
                        assistant_msg["sql"] = parsed["sql"]

                    if parsed.get("error"):
                        with st.expander("⚠️ Query Warning"):
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
                    timeout_text = f"⏱️ The query timed out after 3 minutes (last state: {last_seen}). The Genie may be overloaded — try again in a moment or rephrase your question."
                    st.warning(timeout_text)
                    assistant_msg = {"role": "assistant", "content": timeout_text}

            except Exception as e:
                progress.empty()
                error_text = f"Error communicating with Genie: {str(e)}"
                st.error(error_text)
                assistant_msg = {"role": "assistant", "content": error_text}

            st.session_state.messages.append(assistant_msg)


if __name__ == "__main__":
    main()
