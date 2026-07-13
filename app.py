"""Content Ops Genie — Chat interface for content org data queries."""

import os
import streamlit as st
from genie_client import GenieClient

GENIE_SPACES = {
    "TechOps Tool": os.environ.get("GENIE_SPACE_TECHOPS", "01f0f656e427147884da9fe5344da78f"),
    "Dupe Checker V2": os.environ.get("GENIE_SPACE_DUPE_CHECKER", "01f122f4e2921b7a9c28ef03d0812ee6"),
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


def init_session_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = None
    if "active_space" not in st.session_state:
        st.session_state.active_space = list(GENIE_SPACES.keys())[0]


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
        for name in GENIE_SPACES:
            icon = "✅" if name == st.session_state.active_space else "○"
            st.caption(f"{icon} {name}")

    st.header(f"💬 {st.session_state.active_space}")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
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

        with st.chat_message("assistant"):
            with st.spinner("Querying Genie..."):
                client = get_genie_client()
                space_id = GENIE_SPACES[st.session_state.active_space]

                response = client.ask(
                    space_id=space_id,
                    message=prompt,
                    conversation_id=st.session_state.conversation_id,
                )

                parsed = client.parse_response(response)
                st.session_state.conversation_id = parsed["conversation_id"]

                if parsed["status"] == "COMPLETED":
                    answer = parsed["text"] or "Query completed but no text response was returned."
                    st.markdown(answer)

                    assistant_msg = {"role": "assistant", "content": answer}

                    if parsed["sql"]:
                        with st.expander("SQL Query"):
                            st.code(parsed["sql"], language="sql")
                        assistant_msg["sql"] = parsed["sql"]

                    if parsed["suggested_questions"]:
                        st.caption("**Suggested questions:**")
                        for q in parsed["suggested_questions"]:
                            st.caption(f"• {q}")
                        assistant_msg["suggestions"] = parsed["suggested_questions"]

                elif parsed["status"] == "FAILED":
                    error_text = "The query failed. Please try rephrasing your question."
                    st.error(error_text)
                    assistant_msg = {"role": "assistant", "content": error_text}

                else:
                    timeout_text = "The query timed out. Try a simpler question or try again."
                    st.warning(timeout_text)
                    assistant_msg = {"role": "assistant", "content": timeout_text}

                st.session_state.messages.append(assistant_msg)


if __name__ == "__main__":
    main()
