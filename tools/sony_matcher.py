"""Content ID Matcher — looks up Tubi content IDs by title from the live database."""

import csv
import io
import re
import unicodedata

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

CONTENT_TABLE = "core_prod.content.content_info"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_year(text: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", text).strip()


@st.cache_resource
def _get_workspace_client():
    return WorkspaceClient()


@st.cache_data(ttl=3600)
def _query_content_info():
    """Fetch title/content_id/content_type/import_id from content_info.

    Cached for 1 hour to avoid hammering the warehouse on every match.
    Returns only program-level rows (not episodes) for cleaner matching.
    """
    w = _get_workspace_client()
    result = w.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body={
            "statement": f"""
                SELECT DISTINCT
                    content_id,
                    COALESCE(title, content_name) AS title,
                    content_type,
                    import_id
                FROM {CONTENT_TABLE}
                WHERE content_type IN ('MOVIE', 'SERIES')
                  AND is_episode = false
                  AND active = true
            """,
            "warehouse_id": "a3ea80e7317d51e7",
            "wait_timeout": "60s",
        },
    )

    status = result.get("status", {}).get("state")
    if status != "SUCCEEDED":
        error = result.get("status", {}).get("error", {}).get("message", "Unknown error")
        raise RuntimeError(f"Query failed: {error}")

    columns = [c["name"] for c in result.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = result.get("result", {}).get("data_array", [])
    return [dict(zip(columns, row)) for row in rows]


def _build_index(rows):
    """Build normalized title -> list of records index."""
    index = {}
    for row in rows:
        if not row.get("title"):
            continue
        key = _normalize(row["title"])
        index.setdefault(key, []).append(row)
    return index


def match_title(title: str, title_type: str, index: dict) -> dict:
    """Match a single title against the index.

    Returns {input, type, content_id, import_id, status}
    """
    norm = _normalize(title)

    candidates = index.get(norm, [])
    if title_type:
        typed = [c for c in candidates if c["content_type"] == title_type]
        if typed:
            candidates = typed

    if candidates:
        if len(candidates) == 1:
            c = candidates[0]
            return {
                "input": title,
                "type": c["content_type"],
                "content_id": c["content_id"],
                "import_id": c["import_id"],
                "status": "Match",
            }
        ids = " / ".join(sorted(set(c["content_id"] for c in candidates)))
        imports = " / ".join(sorted(set(c["import_id"] or "" for c in candidates)))
        return {
            "input": title,
            "type": candidates[0]["content_type"],
            "content_id": ids,
            "import_id": imports,
            "status": "Multiple matches - verify",
        }

    stripped = _normalize(_strip_year(title))
    if stripped != norm:
        candidates = index.get(stripped, [])
        if title_type:
            typed = [c for c in candidates if c["content_type"] == title_type]
            if typed:
                candidates = typed

        if candidates:
            if len(candidates) == 1:
                c = candidates[0]
                return {
                    "input": title,
                    "type": c["content_type"],
                    "content_id": c["content_id"],
                    "import_id": c["import_id"],
                    "status": "Fuzzy",
                }
            ids = " / ".join(sorted(set(c["content_id"] for c in candidates)))
            imports = " / ".join(sorted(set(c["import_id"] or "" for c in candidates)))
            return {
                "input": title,
                "type": candidates[0]["content_type"],
                "content_id": ids,
                "import_id": imports,
                "status": "Multiple matches - verify",
            }

    return {
        "input": title,
        "type": title_type or "",
        "content_id": "NEW",
        "import_id": "",
        "status": "NEW",
    }


def parse_input_lines(text: str) -> list:
    """Parse input text into list of (title, type) tuples.

    Handles multi-column CSVs with quoted fields — always extracts
    column 1 as title and column 2 as type (if MOVIE/SERIES).
    """
    results = []
    reader = csv.reader(io.StringIO(text.strip()))
    for row in reader:
        if not row or not row[0].strip():
            continue
        title = row[0].strip()
        if title.lower() == "title":
            continue
        title_type = ""
        if len(row) > 1:
            col2 = row[1].strip().upper()
            if col2 in ("MOVIE", "SERIES"):
                title_type = col2
        results.append((title, title_type))
    return results


def render_content_id_matcher():
    """Render the Content ID Matcher tool UI in Streamlit."""
    st.markdown("""
    <div style="padding: 12px 0 4px;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.02em; color:#1b1626;">
            Content ID Matcher
        </div>
        <div style="font-size:13px; color:#8a8199; margin-top:4px;">
            Paste or upload titles to look up their Tubi content IDs from the live database.
        </div>
    </div>
    """, unsafe_allow_html=True)

    try:
        db_rows = _query_content_info()
        index = _build_index(db_rows)
        st.markdown(f"""
        <div style="font-size:12px; color:#444; background:#f3f0f8; border-radius:8px; padding:8px 14px; display:inline-block; margin:8px 0 16px;">
            Live database: {len(db_rows):,} active titles (movies + series) · refreshes hourly
        </div>
        """, unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Failed to load content database: {e}")
        return

    col_input, col_upload = st.columns([3, 1])
    with col_input:
        input_text = st.text_area(
            "Paste titles (one per line)",
            placeholder="Carmen (2023)\nLost Girl\nSome New Title",
            height=150,
            key="matcher_input",
        )
    with col_upload:
        uploaded = st.file_uploader("Or upload CSV/TXT", type=["csv", "txt"], key="matcher_file")
        if uploaded:
            input_text = uploaded.read().decode("utf-8")

    if st.button("Match Titles", type="primary", key="matcher_btn"):
        if input_text and input_text.strip():
            parsed = parse_input_lines(input_text)
            results = [match_title(title, ttype, index) for title, ttype in parsed]
            st.session_state.matcher_results = results
        else:
            st.warning("Paste or upload titles first.")

    if "matcher_results" in st.session_state and st.session_state.matcher_results:
        results = st.session_state.matcher_results
        df = pd.DataFrame(results)
        df.index = range(1, len(df) + 1)
        df.index.name = "#"

        counts = {
            "Match": len([r for r in results if r["status"] == "Match"]),
            "Fuzzy": len([r for r in results if r["status"] == "Fuzzy"]),
            "NEW": len([r for r in results if r["status"] == "NEW"]),
            "Multiple": len([r for r in results if "Multiple" in r["status"]]),
        }
        cols = st.columns(4)
        for col, (label, count) in zip(cols, counts.items()):
            col.metric(label, count)

        st.dataframe(
            df.style.applymap(
                lambda v: "color: #b3261e; font-weight: 600" if v == "NEW" else "",
                subset=["content_id"],
            ),
            use_container_width=True,
        )

        col_dl, col_copy = st.columns([1, 1])
        with col_dl:
            csv_out = df.to_csv()
            st.download_button("Download results (CSV)", csv_out, "content_id_matches.csv", "text/csv")
        with col_copy:
            ids = "\n".join(r["content_id"] for r in results)
            st.code(ids, language=None)
