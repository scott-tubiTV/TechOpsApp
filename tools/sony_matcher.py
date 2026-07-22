"""Content ID Matcher — looks up Tubi content IDs by title from the live database."""

import csv
import io
import re
import unicodedata

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

AVAIL_MOVIES = "core_prod.contentavails_cdc.avail_movies"
AVAIL_SERIES = "core_prod.contentavails_cdc.avail_series"


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


WAREHOUSE_ID = "a6b9541289d75c6e"


def _execute_sql(query):
    """Execute a SQL query and return rows as list of dicts."""
    import time

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
        error = result.get("status", {}).get("error", {}).get("message", "Unknown error")
        raise RuntimeError(f"Query failed: {error}")

    columns = [c["name"] for c in result.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = result.get("result", {}).get("data_array", [])
    return [dict(zip(columns, row)) for row in rows]


@st.cache_data(ttl=3600)
def _query_import_ids():
    """Fetch all distinct import_ids from avails tables."""
    rows = _execute_sql(f"""
        SELECT DISTINCT import_id FROM (
            SELECT import_id FROM {AVAIL_MOVIES} WHERE import_id IS NOT NULL AND import_id != ''
            UNION
            SELECT import_id FROM {AVAIL_SERIES} WHERE import_id IS NOT NULL AND import_id != ''
        )
        ORDER BY import_id
    """)
    return [r["import_id"] for r in rows]


@st.cache_data(ttl=3600)
def _query_avails(import_id):
    """Fetch titles from avail_movies + avail_series for a specific import_id.

    Cached for 1 hour. Uses the original avails titles (pre-import rename).
    """
    return _execute_sql(f"""
        SELECT DISTINCT content_id, title, 'MOVIE' AS content_type, import_id
        FROM {AVAIL_MOVIES}
        WHERE import_id = '{import_id}'
          AND content_id IS NOT NULL
        UNION ALL
        SELECT DISTINCT content_id, title, 'SERIES' AS content_type, import_id
        FROM {AVAIL_SERIES}
        WHERE import_id = '{import_id}'
          AND content_id IS NOT NULL
    """)


CONTENT_INFO = "core_prod.content.content_info"


def _check_active_status(content_ids):
    """Query content_info.active for a batch of content_ids. Returns {content_id: bool}."""
    if not content_ids:
        return {}
    id_list = ", ".join(f"'{cid}'" for cid in content_ids)
    rows = _execute_sql(f"""
        SELECT content_id, active
        FROM {CONTENT_INFO}
        WHERE content_id IN ({id_list})
    """)
    return {r["content_id"]: str(r["active"]).lower() == "true" for r in rows}


def _resolve_multiple_matches(results):
    """Post-process results: auto-resolve multiples where only one content_id is active."""
    multi_indices = [
        i for i, r in enumerate(results)
        if r["status"] == "Multiple matches - verify"
    ]
    if not multi_indices:
        return results

    all_cids = set()
    for i in multi_indices:
        for cid in results[i]["content_id"].split(" / "):
            all_cids.add(cid.strip())

    active_map = _check_active_status(list(all_cids))

    for i in multi_indices:
        cids = [cid.strip() for cid in results[i]["content_id"].split(" / ")]
        active_cids = [cid for cid in cids if active_map.get(cid, False)]
        if len(active_cids) == 1:
            results[i]["content_id"] = active_cids[0]
            results[i]["status"] = "Match"

    return results


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

    # Import ID selector
    try:
        import_ids = _query_import_ids()
    except Exception as e:
        st.error(f"Failed to load import IDs: {e}")
        return

    selected_import_id = st.selectbox(
        "Select import_id to match against",
        import_ids,
        index=import_ids.index("sony-pictures") if "sony-pictures" in import_ids else 0,
        key="matcher_import_id",
    )

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
            try:
                with st.spinner(f"Loading titles for {selected_import_id}..."):
                    db_rows = _query_avails(selected_import_id)
                    index = _build_index(db_rows)
                parsed = parse_input_lines(input_text)
                results = [match_title(title, ttype, index) for title, ttype in parsed]
                results = _resolve_multiple_matches(results)
                st.session_state.matcher_results = results
            except Exception as e:
                st.error(f"Failed to load content database: {e}")
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

        matched = [r for r in results if r["status"] in ("Match", "Fuzzy", "Multiple matches - verify")]
        new_only = [r for r in results if r["status"] == "NEW"]

        col1, col2, col3 = st.columns(3)
        with col1:
            csv_all = df.to_csv()
            st.download_button("Download all results", csv_all, "content_id_matches.csv", "text/csv")
        with col2:
            if matched:
                df_matched = pd.DataFrame(matched)
                st.download_button("Download matches only", df_matched.to_csv(index=False), "matched_titles.csv", "text/csv")
        with col3:
            if new_only:
                df_new = pd.DataFrame(new_only)
                st.download_button("Download NEW only", df_new.to_csv(index=False), "new_titles.csv", "text/csv")
