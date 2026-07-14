"""Content Ops Genie — Chat interface for content org data queries."""

import os
import pandas as pd
import streamlit as st
from genie_client import GenieClient

GENIE_SPACES = {
    "TechOps Tool": {
        "id": os.environ.get("GENIE_SPACE_TECHOPS", "01f0f656e427147884da9fe5344da78f"),
        "description": "Content pipeline queries: ABF status, avails, metadata, policies, redeliveries",
        "keywords": ["abf", "avail", "content", "policy", "metadata", "redelivery", "delivery", "import", "series", "episode", "movie", "partner", "titan", "assessment"],
    },
    "Dupe Checker V2": {
        "id": os.environ.get("GENIE_SPACE_DUPE_CHECKER", "01f122f4e2921b7a9c28ef03d0812ee6"),
        "description": "Duplicate title detection: upload CSV avails to check for existing titles and conflicts",
        "keywords": ["dupe", "duplicate", "conflict", "csv", "upload", "check", "avails file", "overlap", "territory"],
    },
}

st.set_page_config(
    page_title="Content Ops Genie",
    page_icon="🔮",
    layout="wide",
)


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
        st.title("Content Ops Genie")
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

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
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

        with st.chat_message("assistant"):
            with st.spinner(f"Querying {routed_space}..."):
                client = get_genie_client()
                space_id = GENIE_SPACES[routed_space]["id"]

                try:
                    response = client.ask(
                        space_id=space_id,
                        message=prompt,
                        conversation_id=st.session_state.conversation_id,
                    )

                    parsed = client.parse_response(response)
                    st.session_state.conversation_id = parsed["conversation_id"]

                    if parsed["status"] == "COMPLETED":
                        answer = parsed["text"] or parsed.get("query_description") or ""

                        # Fetch query results if available
                        query_df = None
                        if parsed.get("_query_attachment_id"):
                            try:
                                qr = client.get_query_result(
                                    space_id=space_id,
                                    conversation_id=parsed["conversation_id"],
                                    message_id=response.get("id", ""),
                                    attachment_id=parsed["_query_attachment_id"],
                                )
                                columns = [col["name"] for col in qr.get("statement_response", {}).get("manifest", {}).get("schema", {}).get("columns", [])]
                                rows = []
                                for chunk in qr.get("statement_response", {}).get("result", {}).get("data_typed_array", []):
                                    row = [v.get("str", v.get("value", "")) for v in chunk.get("values", [])]
                                    rows.append(row)
                                if not rows:
                                    for chunk in qr.get("statement_response", {}).get("result", {}).get("data_array", []):
                                        rows.append(chunk)
                                if columns and rows:
                                    query_df = pd.DataFrame(rows, columns=columns)
                            except Exception:
                                pass

                        if answer:
                            st.markdown(answer)
                        if query_df is not None:
                            st.dataframe(query_df, use_container_width=True)
                        elif not answer:
                            st.info("Query completed but no results were returned.")

                        display_text = answer or ("Query returned results" if query_df is not None else "No results")
                        assistant_msg = {"role": "assistant", "content": display_text}

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
                        with st.expander("Debug Info"):
                            st.caption(f"Raw status: `{parsed.get('raw_status')}`")
                            st.caption(f"Conversation ID: `{parsed.get('conversation_id')}`")
                        assistant_msg = {"role": "assistant", "content": error_text}

                    else:
                        timeout_text = "The query timed out. Try a simpler question or try again."
                        st.warning(timeout_text)
                        with st.expander("Debug Info"):
                            st.caption(f"Raw status: `{parsed.get('raw_status')}`")
                        assistant_msg = {"role": "assistant", "content": timeout_text}

                except Exception as e:
                    error_text = f"Error communicating with Genie: {str(e)}"
                    st.error(error_text)
                    assistant_msg = {"role": "assistant", "content": error_text}

                st.session_state.messages.append(assistant_msg)


if __name__ == "__main__":
    main()
