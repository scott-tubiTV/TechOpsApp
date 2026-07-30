"""IMDB Dupe Checker — matches titles to IMDB IDs and detects duplicates/policy conflicts."""

import csv
import io
import re
import unicodedata

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

CONTENT_INFO = "core_prod.tubidw.content_info"
RICH_CONTENT = "ml_prod.common.rich_content"
IMDB_CATALOG = "core_prod.imdb.imdb_title_essential_v2"
POLICY_WINDOWS = "core_prod.policydb_cdc.content_policy_windows"
WAREHOUSE_ID = "a6b9541289d75c6e"


@st.cache_resource
def _get_workspace_client():
    return WorkspaceClient()


def _execute_sql(query):
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


def _normalize(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_no_article(text):
    norm = _normalize(text)
    for article in ("the ", "a ", "an "):
        if norm.startswith(article):
            return norm[len(article):]
    for article in (" the", " a", " an"):
        if norm.endswith(article):
            return norm[: -len(article)]
    return norm


def _escape(val):
    if val is None:
        return "NULL"
    return str(val).replace("\\", "\\\\").replace("'", "''")


def parse_csv_input(text):
    """Parse CSV input into list of dicts with title, type, release_year."""
    results = []
    reader = csv.reader(io.StringIO(text.strip()))
    rows_list = list(reader)
    if not rows_list:
        return results

    col_map = {}
    first_row = [c.strip().lower() for c in rows_list[0]]
    header_keywords = {
        "title": ["title", "name", "content_name"],
        "type": ["type", "prod type", "content type", "content_type"],
        "release_year": ["release year", "release_year", "year"],
    }
    for field, keywords in header_keywords.items():
        for i, col in enumerate(first_row):
            if col in keywords:
                col_map[field] = i
                break

    has_header = "title" in col_map
    start_idx = 1 if has_header else 0
    if not has_header:
        col_map = {"title": 0}

    for row in rows_list[start_idx:]:
        if not row or not row[0].strip():
            continue
        title_idx = col_map.get("title", 0)
        if title_idx >= len(row):
            continue
        title = row[title_idx].strip()
        if not title or title.lower() == "title":
            continue

        title_type = ""
        type_idx = col_map.get("type")
        if type_idx is not None and type_idx < len(row):
            val = row[type_idx].strip().upper()
            if val in ("MOVIE", "SERIES"):
                title_type = val
            elif val in ("FEATURE", "DTV/FT FGN REL", "DTV/FT US MIN"):
                title_type = "MOVIE"

        release_year = ""
        year_idx = col_map.get("release_year")
        if year_idx is not None and year_idx < len(row):
            val = row[year_idx].strip()
            if re.match(r"^\d{4}$", val):
                release_year = val

        results.append({"title": title, "type": title_type, "release_year": release_year})
    return results


def _step1_match_content_ids(titles):
    """Match titles against content_info to find content_ids.

    Returns list of dicts with original fields + content_id, ci_title, ci_year, match_quality.
    """
    if not titles:
        return []

    title_values = []
    for t in titles:
        norm = _normalize(t["title"])
        norm_no_art = _normalize_no_article(t["title"])
        title_values.append(f"('{_escape(t['title'])}', '{_escape(norm)}', '{_escape(norm_no_art)}', '{_escape(t['type'])}', '{_escape(t['release_year'])}')")

    values_sql = ",\n".join(title_values)

    rows = _execute_sql(f"""
        WITH input_titles AS (
            SELECT col1 AS original_title, col2 AS norm_title, col3 AS norm_no_article, col4 AS content_type, col5 AS release_year
            FROM (VALUES {values_sql}) AS t(col1, col2, col3, col4, col5)
        ),
        ci_normalized AS (
            SELECT
                content_id,
                title,
                content_type,
                release_year,
                active,
                LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(title, '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_title,
                LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(
                    REGEXP_REPLACE(title, '(?i)^(the|a|an)\\\\s+', ''),
                    '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_no_article
            FROM {CONTENT_INFO}
            WHERE active = true
              AND content_type IN ('MOVIE', 'SERIES')
        )
        SELECT
            it.original_title,
            it.content_type AS input_type,
            it.release_year AS input_year,
            ci.content_id,
            ci.title AS ci_title,
            ci.content_type AS ci_type,
            ci.release_year AS ci_year,
            CASE
                WHEN it.norm_title = ci.norm_title THEN 'exact'
                WHEN it.norm_no_article = ci.norm_no_article THEN 'no_article'
                ELSE 'fuzzy'
            END AS match_quality
        FROM input_titles it
        JOIN ci_normalized ci
            ON (it.norm_title = ci.norm_title OR it.norm_no_article = ci.norm_no_article)
        WHERE (it.content_type = '' OR it.content_type = ci.content_type)
          AND (it.release_year = '' OR it.release_year = CAST(ci.release_year AS STRING)
               OR ABS(CAST(it.release_year AS INT) - ci.release_year) <= 1)
    """)
    return rows


def _step2_imdb_from_content_ids(content_ids):
    """Look up IMDB IDs via rich_content for resolved content_ids."""
    if not content_ids:
        return {}
    id_list = ", ".join(f"'{cid}'" for cid in content_ids)
    rows = _execute_sql(f"""
        SELECT tubi_video_id AS content_id, imdb_id
        FROM {RICH_CONTENT}
        WHERE tubi_video_id IN ({id_list})
          AND imdb_id IS NOT NULL
          AND imdb_id != ''
    """)
    return {r["content_id"]: r["imdb_id"] for r in rows}


def _step3_fuzzy_imdb_match(titles):
    """Fuzzy match titles directly against IMDB catalog.

    Returns list of dicts with original_title, imdb_id, imdb_title, imdb_year, confidence.
    """
    if not titles:
        return []

    title_values = []
    for t in titles:
        norm = _normalize(t["title"])
        norm_no_art = _normalize_no_article(t["title"])
        title_values.append(f"('{_escape(t['title'])}', '{_escape(norm)}', '{_escape(norm_no_art)}', '{_escape(t['type'])}', '{_escape(t['release_year'])}')")

    values_sql = ",\n".join(title_values)

    rows = _execute_sql(f"""
        WITH input_titles AS (
            SELECT col1 AS original_title, col2 AS norm_title, col3 AS norm_no_article, col4 AS content_type, col5 AS release_year
            FROM (VALUES {values_sql}) AS t(col1, col2, col3, col4, col5)
        ),
        imdb_norm AS (
            SELECT
                titleId AS imdb_id,
                originalTitle,
                year AS imdb_year,
                titleType,
                CASE
                    WHEN titleType IN ('movie', 'tvMovie', 'video') THEN 'MOVIE'
                    WHEN titleType IN ('tvSeries', 'tvMiniSeries') THEN 'SERIES'
                END AS mapped_type,
                LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(originalTitle, '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_title,
                LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(
                    REGEXP_REPLACE(originalTitle, '(?i)^(the|a|an)\\\\s+', ''),
                    '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_no_article
            FROM {IMDB_CATALOG}
            WHERE titleType IN ('movie', 'tvMovie', 'video', 'tvSeries', 'tvMiniSeries')
        ),
        matched AS (
            SELECT
                it.original_title,
                it.release_year AS input_year,
                it.content_type AS input_type,
                imdb.imdb_id,
                imdb.originalTitle AS imdb_title,
                imdb.imdb_year,
                imdb.titleType AS imdb_type,
                CASE
                    WHEN it.norm_title = imdb.norm_title THEN 'exact'
                    WHEN it.norm_no_article = imdb.norm_no_article THEN 'no_article'
                    ELSE 'other'
                END AS title_match,
                CASE
                    WHEN it.release_year != '' AND imdb.imdb_year IS NOT NULL
                         AND CAST(it.release_year AS INT) = imdb.imdb_year THEN 'exact_year'
                    WHEN it.release_year != '' AND imdb.imdb_year IS NOT NULL
                         AND ABS(CAST(it.release_year AS INT) - imdb.imdb_year) <= 1 THEN 'year_off_by_1'
                    WHEN it.release_year = '' OR imdb.imdb_year IS NULL THEN 'year_missing'
                    ELSE 'year_mismatch'
                END AS year_match
            FROM input_titles it
            JOIN imdb_norm imdb
                ON (it.norm_title = imdb.norm_title OR it.norm_no_article = imdb.norm_no_article)
            WHERE (it.content_type = '' OR it.content_type = imdb.mapped_type)
              AND NOT (
                  it.release_year != '' AND imdb.imdb_year IS NOT NULL
                  AND ABS(CAST(it.release_year AS INT) - imdb.imdb_year) > 1
              )
        ),
        candidate_counts AS (
            SELECT original_title, COUNT(DISTINCT imdb_id) AS num_candidates
            FROM matched
            GROUP BY original_title
        ),
        ranked AS (
            SELECT
                m.*,
                cc.num_candidates,
                ROW_NUMBER() OVER (
                    PARTITION BY m.original_title
                    ORDER BY
                        CASE WHEN m.year_match = 'exact_year' THEN 0
                             WHEN m.year_match = 'year_off_by_1' THEN 1
                             WHEN m.year_match = 'year_missing' THEN 2
                             ELSE 3 END,
                        CASE WHEN m.title_match = 'exact' THEN 0 ELSE 1 END
                ) AS rn
            FROM matched m
            JOIN candidate_counts cc ON m.original_title = cc.original_title
        )
        SELECT
            original_title,
            input_year,
            input_type,
            imdb_id,
            imdb_title,
            imdb_year,
            imdb_type,
            title_match,
            year_match,
            num_candidates,
            CASE
                WHEN num_candidates = 1 AND year_match = 'exact_year' AND title_match = 'exact' THEN 'HIGH'
                WHEN num_candidates = 1 AND year_match IN ('exact_year', 'year_off_by_1') THEN 'HIGH'
                WHEN num_candidates = 1 AND year_match = 'year_missing' AND title_match = 'exact' THEN 'MEDIUM'
                WHEN num_candidates = 1 THEN 'MEDIUM'
                WHEN num_candidates <= 3 AND year_match = 'exact_year' THEN 'MEDIUM'
                ELSE 'LOW'
            END AS confidence
        FROM ranked
        WHERE rn = 1
    """)
    return rows


def _step4_find_duplicates(imdb_ids):
    """Check if any IMDB IDs already exist in the backend under different content_ids."""
    if not imdb_ids:
        return {}
    id_list = ", ".join(f"'{_escape(iid)}'" for iid in imdb_ids)
    rows = _execute_sql(f"""
        SELECT
            rc.imdb_id,
            rc.tubi_video_id AS existing_content_id,
            ci.title AS existing_title,
            ci.active,
            ci.content_type,
            ci.import_id
        FROM {RICH_CONTENT} rc
        JOIN {CONTENT_INFO} ci ON ci.content_id = rc.tubi_video_id
        WHERE rc.imdb_id IN ({id_list})
          AND rc.tubi_video_id IS NOT NULL
    """)
    dupes = {}
    for r in rows:
        dupes.setdefault(r["imdb_id"], []).append(r)
    return dupes


def _step5_check_policy_conflicts(content_ids):
    """Check for active policy windows on content_ids."""
    if not content_ids:
        return {}
    id_list = ", ".join(f"'{cid}'" for cid in content_ids)
    rows = _execute_sql(f"""
        SELECT
            CAST(content_id AS STRING) AS content_id,
            timespan,
            country_list,
            deal_id
        FROM {POLICY_WINDOWS}
        WHERE CAST(content_id AS STRING) IN ({id_list})
          AND UPPER(timespan) > CAST(current_timestamp() AS STRING)
    """)
    policies = {}
    for r in rows:
        policies.setdefault(r["content_id"], []).append(r)
    return policies


def _run_pipeline(titles, progress_callback=None):
    """Run the full dupe-check pipeline. Returns combined results."""
    results = []

    if progress_callback:
        progress_callback("Step 1/5: Matching titles to content IDs...")

    # Step 1: Match content_ids from content_info
    ci_matches = _step1_match_content_ids(titles)

    # Build lookup: original_title -> best content_id match
    ci_lookup = {}
    for row in ci_matches:
        key = row["original_title"]
        if key not in ci_lookup or row["match_quality"] == "exact":
            ci_lookup[key] = row

    if progress_callback:
        progress_callback("Step 2/5: Looking up IMDB IDs via rich_content...")

    # Step 2: Get IMDB IDs from rich_content for matched content_ids
    matched_cids = [r["content_id"] for r in ci_lookup.values()]
    imdb_from_rc = _step2_imdb_from_content_ids(matched_cids)

    if progress_callback:
        progress_callback("Step 3/5: Fuzzy matching against IMDB catalog...")

    # Step 3: Fuzzy match ALL titles against IMDB catalog (tandem approach)
    imdb_fuzzy = _step3_fuzzy_imdb_match(titles)
    imdb_fuzzy_lookup = {}
    for row in imdb_fuzzy:
        imdb_fuzzy_lookup[row["original_title"]] = row

    if progress_callback:
        progress_callback("Step 4/5: Checking for duplicates...")

    # Collect all recovered IMDB IDs for dupe detection
    all_imdb_ids = set()
    for iid in imdb_from_rc.values():
        all_imdb_ids.add(iid)
    for row in imdb_fuzzy:
        all_imdb_ids.add(row["imdb_id"])

    dupes = _step4_find_duplicates(list(all_imdb_ids))

    if progress_callback:
        progress_callback("Step 5/5: Checking policy conflicts...")

    # Collect content_ids from dupes for policy check
    dupe_cids = set()
    for dupe_list in dupes.values():
        for d in dupe_list:
            dupe_cids.add(d["existing_content_id"])
    policies = _step5_check_policy_conflicts(list(dupe_cids))

    if progress_callback:
        progress_callback("Building results...")

    # Combine into final results
    for t in titles:
        row = {
            "title": t["title"],
            "type": t["type"],
            "release_year": t["release_year"],
            "content_id": "",
            "content_id_match": "",
            "imdb_id": "",
            "imdb_source": "",
            "imdb_confidence": "",
            "is_duplicate": False,
            "duplicate_content_id": "",
            "duplicate_title": "",
            "duplicate_active": "",
            "duplicate_import_id": "",
            "has_policy_conflict": False,
            "policy_details": "",
        }

        # Content ID from step 1
        ci = ci_lookup.get(t["title"])
        if ci:
            row["content_id"] = ci["content_id"]
            row["content_id_match"] = ci["match_quality"]

        # IMDB ID - prefer rich_content (direct), fall back to fuzzy
        imdb_id = None
        if ci and ci["content_id"] in imdb_from_rc:
            imdb_id = imdb_from_rc[ci["content_id"]]
            row["imdb_id"] = imdb_id
            row["imdb_source"] = "rich_content"
            row["imdb_confidence"] = "HIGH"
        elif t["title"] in imdb_fuzzy_lookup:
            fuzzy = imdb_fuzzy_lookup[t["title"]]
            imdb_id = fuzzy["imdb_id"]
            row["imdb_id"] = imdb_id
            row["imdb_source"] = "imdb_catalog"
            row["imdb_confidence"] = fuzzy["confidence"]

        # Cross-validation: if both paths found IMDB IDs, check agreement
        if ci and ci["content_id"] in imdb_from_rc and t["title"] in imdb_fuzzy_lookup:
            rc_imdb = imdb_from_rc[ci["content_id"]]
            fuzzy_imdb = imdb_fuzzy_lookup[t["title"]]["imdb_id"]
            if rc_imdb == fuzzy_imdb:
                row["imdb_confidence"] = "HIGH (cross-validated)"
            elif rc_imdb != fuzzy_imdb:
                row["imdb_confidence"] = "CONFLICT"
                row["imdb_id"] = f"{rc_imdb} vs {fuzzy_imdb}"

        # Dupe detection
        if imdb_id and imdb_id in dupes:
            dupe_list = dupes[imdb_id]
            # Filter out self (same content_id)
            other_dupes = [d for d in dupe_list if d["existing_content_id"] != row["content_id"]]
            if other_dupes:
                row["is_duplicate"] = True
                best_dupe = next((d for d in other_dupes if str(d.get("active", "")).lower() == "true"), other_dupes[0])
                row["duplicate_content_id"] = best_dupe["existing_content_id"]
                row["duplicate_title"] = best_dupe.get("existing_title", "")
                row["duplicate_active"] = str(best_dupe.get("active", "")).lower() == "true"
                row["duplicate_import_id"] = best_dupe.get("import_id", "")

                # Policy conflict on the duplicate
                if best_dupe["existing_content_id"] in policies:
                    row["has_policy_conflict"] = True
                    windows = policies[best_dupe["existing_content_id"]]
                    row["policy_details"] = f"{len(windows)} active window(s): {windows[0].get('country_list', '')}"

        results.append(row)

    return results


def render_imdb_dupe_checker():
    """Render the IMDB Dupe Checker tool UI in Streamlit."""
    st.markdown("""
    <div style="padding: 12px 0 4px;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.02em; color:#1b1626;">
            IMDB Dupe Checker
        </div>
        <div style="font-size:13px; color:#8a8199; margin-top:4px;">
            Upload titles to find IMDB IDs, detect duplicates already in the backend, and identify policy conflicts.
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div style="background:#f8f7fa; border:1px solid #e7e3ee; border-radius:8px; padding:12px 16px; margin:8px 0 16px;">
        <div style="font-size:12px; color:#6b5f7a; line-height:1.6;">
            <strong>Pipeline:</strong> CSV → Content ID match → IMDB lookup (rich_content + catalog fuzzy) → Duplicate detection → Policy conflict check<br>
            <strong>CSV format:</strong> Title, Type (MOVIE/SERIES), Year — header row optional
        </div>
    </div>
    """, unsafe_allow_html=True)

    col_input, col_upload = st.columns([3, 1])
    with col_input:
        input_text = st.text_area(
            "Paste titles (CSV format)",
            placeholder="Title,Type,Year\nThe Matrix,MOVIE,1999\nBreaking Bad,SERIES,2008\nSome Unknown Film,MOVIE,2020",
            height=150,
            key="dupe_input",
        )
    with col_upload:
        uploaded = st.file_uploader("Or upload CSV", type=["csv", "txt"], key="dupe_file")
        if uploaded:
            input_text = uploaded.read().decode("utf-8")

    if st.button("Run Dupe Check", type="primary", key="dupe_btn"):
        if not input_text or not input_text.strip():
            st.warning("Paste or upload titles first.")
            return

        titles = parse_csv_input(input_text)
        if not titles:
            st.warning("No valid titles found in input.")
            return

        progress = st.empty()

        def update_progress(msg):
            progress.info(msg)

        try:
            results = _run_pipeline(titles, progress_callback=update_progress)
            progress.empty()
            st.session_state.dupe_results = results
        except Exception as e:
            progress.empty()
            st.error(f"Pipeline failed: {e}")
            return

    if "dupe_results" in st.session_state and st.session_state.dupe_results:
        results = st.session_state.dupe_results

        # Summary metrics
        total = len(results)
        matched_imdb = len([r for r in results if r["imdb_id"] and "vs" not in r["imdb_id"]])
        duplicates = len([r for r in results if r["is_duplicate"]])
        conflicts = len([r for r in results if r["has_policy_conflict"]])
        no_match = len([r for r in results if not r["imdb_id"]])

        cols = st.columns(5)
        cols[0].metric("Total", total)
        cols[1].metric("IMDB Matched", matched_imdb)
        cols[2].metric("Duplicates", duplicates)
        cols[3].metric("Policy Conflicts", conflicts)
        cols[4].metric("No IMDB Match", no_match)

        # Tabs for different views
        tab_all, tab_dupes, tab_conflicts, tab_unmatched = st.tabs(
            ["All Results", f"Duplicates ({duplicates})", f"Policy Conflicts ({conflicts})", f"No Match ({no_match})"]
        )

        df = pd.DataFrame(results)
        display_cols = [
            "title", "type", "release_year", "content_id", "imdb_id",
            "imdb_confidence", "is_duplicate", "duplicate_content_id",
            "duplicate_title", "has_policy_conflict", "policy_details",
        ]
        display_cols = [c for c in display_cols if c in df.columns]

        with tab_all:
            st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

        with tab_dupes:
            dupe_df = df[df["is_duplicate"] == True]
            if not dupe_df.empty:
                dupe_cols = [
                    "title", "type", "release_year", "imdb_id",
                    "duplicate_content_id", "duplicate_title",
                    "duplicate_active", "duplicate_import_id",
                    "has_policy_conflict", "policy_details",
                ]
                dupe_cols = [c for c in dupe_cols if c in dupe_df.columns]
                st.dataframe(dupe_df[dupe_cols], use_container_width=True, hide_index=True)
            else:
                st.success("No duplicates found.")

        with tab_conflicts:
            conflict_df = df[df["has_policy_conflict"] == True]
            if not conflict_df.empty:
                conflict_cols = [
                    "title", "imdb_id", "duplicate_content_id",
                    "duplicate_title", "policy_details",
                ]
                conflict_cols = [c for c in conflict_cols if c in conflict_df.columns]
                st.dataframe(conflict_df[conflict_cols], use_container_width=True, hide_index=True)
            else:
                st.success("No policy conflicts found.")

        with tab_unmatched:
            unmatched_df = df[df["imdb_id"] == ""]
            if not unmatched_df.empty:
                st.dataframe(
                    unmatched_df[["title", "type", "release_year", "content_id", "content_id_match"]],
                    use_container_width=True, hide_index=True,
                )
            else:
                st.success("All titles matched to IMDB IDs.")

        # Downloads
        st.divider()
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button(
                "Download all results",
                df.to_csv(index=False),
                "imdb_dupe_check_results.csv",
                "text/csv",
            )
        with col2:
            if duplicates > 0:
                st.download_button(
                    "Download duplicates only",
                    df[df["is_duplicate"] == True].to_csv(index=False),
                    "duplicates.csv",
                    "text/csv",
                )
        with col3:
            if no_match > 0:
                st.download_button(
                    "Download unmatched only",
                    df[df["imdb_id"] == ""].to_csv(index=False),
                    "unmatched_titles.csv",
                    "text/csv",
                )
