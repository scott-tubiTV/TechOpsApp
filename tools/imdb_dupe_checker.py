"""IMDB Dupe Checker — matches titles to IMDB IDs and detects duplicates/policy conflicts."""

import csv
import io
import re
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

from imdb_engine import (
    IMDBEngineConfig,
    IMDBMatcher,
    IMDBVerifier,
    MatchMethod,
    MatchRequest,
    MatchResult,
    SQLClient,
    VerificationRecord,
    Confidence,
)
from imdb_engine.normalizer import escape_sql

POLICY_WINDOWS = "core_prod.policydb_cdc.content_policy_windows"
RICH_CONTENT = "ml_prod.common.rich_content"
CONTENT_INFO = "core_prod.tubidw.content_info"


@st.cache_resource
def _get_workspace_client():
    return WorkspaceClient()


@st.cache_resource
def _get_engine():
    w = _get_workspace_client()
    config = IMDBEngineConfig()
    sql = SQLClient(workspace_client=w, config=config)
    matcher = IMDBMatcher(config=config, sql_client=sql)
    verifier = IMDBVerifier(sql_client=sql, config=config)
    try:
        verifier.ensure_table_exists()
    except Exception:
        pass
    return matcher, verifier, sql, config


def _get_user_email():
    try:
        user_info = st.experimental_user
        if user_info and user_info.get("email"):
            return user_info["email"]
    except Exception:
        pass
    headers = st.context.headers
    for h in ["X-Forwarded-Email", "X-Forwarded-Preferred-Username", "X-Databricks-User-Email"]:
        val = headers.get(h)
        if val:
            return val
    return "unknown"


def parse_csv_input(text):
    """Parse CSV input into list of MatchRequests."""
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
        "director": ["director"],
        "imdb_id": ["imdb_id", "imdb id", "imdb"],
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

        content_type = ""
        type_idx = col_map.get("type")
        if type_idx is not None and type_idx < len(row):
            val = row[type_idx].strip().upper()
            if val in ("MOVIE", "SERIES"):
                content_type = val
            elif val in ("FEATURE", "DTV/FT FGN REL", "DTV/FT US MIN"):
                content_type = "MOVIE"

        release_year = ""
        year_idx = col_map.get("release_year")
        if year_idx is not None and year_idx < len(row):
            val = row[year_idx].strip()
            if re.match(r"^\d{4}$", val):
                release_year = val

        director = ""
        dir_idx = col_map.get("director")
        if dir_idx is not None and dir_idx < len(row):
            director = row[dir_idx].strip()

        # Pre-extracted IMDB ID (from Excel URL parsing)
        known_imdb = ""
        imdb_idx = col_map.get("imdb_id")
        if imdb_idx is not None and imdb_idx < len(row):
            val = row[imdb_idx].strip()
            if val.startswith("tt"):
                known_imdb = val

        req = MatchRequest(
            title=title,
            content_type=content_type,
            release_year=release_year,
            director=director,
        )
        # Attach known IMDB ID as metadata for the pipeline
        req._known_imdb_id = known_imdb
        results.append(req)
    return results


def _parse_excel_to_csv(uploaded_file):
    """Read an Excel file and convert to CSV text for parse_csv_input.

    Handles messy partner avails files with banner rows and merged headers.
    Detects the real header row by looking for a cell containing 'TITLE'.
    Extracts IMDB IDs from URL columns if present.
    """
    raw = pd.read_excel(uploaded_file, dtype=str, engine="openpyxl", header=None)

    # Find the header row — look for a row containing "TITLE"
    header_row = 0
    for i in range(min(20, len(raw))):
        row_vals = [str(v).strip().upper() for v in raw.iloc[i].tolist() if str(v) != "nan"]
        if "TITLE" in row_vals:
            header_row = i
            break

    # Use that row as headers, data starts after it (skip one more if it's a sub-header)
    headers = [str(h).strip() if str(h) != "nan" else f"col_{j}" for j, h in enumerate(raw.iloc[header_row].tolist())]
    data_start = header_row + 1

    # Check if row after header is also a sub-header (e.g., section divider)
    if data_start < len(raw):
        first_data = raw.iloc[data_start].tolist()
        non_null = [str(v) for v in first_data if str(v) != "nan"]
        # If very few values and looks like a section label, skip it
        if len(non_null) <= 2:
            data_start += 1

    df = raw.iloc[data_start:].copy()
    df.columns = headers

    # Normalize column names for mapping
    col_lower = {col: col.strip().lower().rstrip() for col in df.columns}

    # Map to our expected format
    col_renames = {}
    imdb_col = None
    for col, lower in col_lower.items():
        if lower in ("title", "name", "content_name", "movie title", "series title"):
            col_renames[col] = "title"
        elif lower in ("type", "prod type", "content type", "content_type", "category"):
            col_renames[col] = "type"
        elif lower in ("release year", "release_year", "year", "rls", "rls year"):
            col_renames[col] = "year"
        elif lower in ("director", "directors"):
            col_renames[col] = "director"
        elif "imdb" in lower:
            imdb_col = col

    df = df.rename(columns=col_renames)

    # Filter out rows without a title
    if "title" in df.columns:
        df = df[df["title"].notna() & (df["title"].str.strip() != "")]
    else:
        return ""

    # Extract IMDB ID from URL column if present
    if imdb_col and imdb_col not in col_renames:
        df["imdb_id"] = df[imdb_col].apply(_extract_imdb_id_from_url)

    # Map category values to MOVIE/SERIES type if we used category as type
    if "type" in df.columns:
        df["type"] = df["type"].apply(_map_category_to_type)

    # Select only the columns we care about
    keep_cols = [c for c in ["title", "type", "year", "director", "imdb_id"] if c in df.columns]
    return df[keep_cols].to_csv(index=False)


def _extract_imdb_id_from_url(val):
    """Extract ttXXXXXXX from an IMDB URL."""
    if not val or str(val) == "nan":
        return ""
    match = re.search(r"(tt\d{7,})", str(val))
    return match.group(1) if match else ""


def _map_category_to_type(val):
    """Map partner category labels to MOVIE/SERIES."""
    if not val or str(val) == "nan":
        return ""
    val_lower = str(val).strip().lower()
    if "series" in val_lower or "tv" in val_lower:
        return "SERIES"
    if "film" in val_lower or "movie" in val_lower:
        return "MOVIE"
    return ""


def _find_duplicates(imdb_ids, sql_client, config):
    """Check if IMDB IDs already exist in backend under different content_ids."""
    if not imdb_ids:
        return {}
    id_list = ", ".join(f"'{escape_sql(iid)}'" for iid in imdb_ids)

    # Try rich_content first (most complete mapping), fall back to content_info
    try:
        rows = sql_client.execute(f"""
            SELECT
                rc.imdb_id,
                rc.tubi_video_id AS existing_content_id,
                ci.title AS existing_title,
                ci.active,
                ci.content_type,
                ci.import_id
            FROM {config.rich_content_table} rc
            JOIN {config.content_info_table} ci ON ci.content_id = rc.tubi_video_id
            WHERE rc.imdb_id IN ({id_list})
              AND rc.tubi_video_id IS NOT NULL
        """)
    except Exception:
        # Fallback: use content_info imdb_id/program_imdb_id directly
        rows = sql_client.execute(f"""
            SELECT
                COALESCE(imdb_id, program_imdb_id) AS imdb_id,
                content_id AS existing_content_id,
                title AS existing_title,
                active,
                content_type,
                import_id
            FROM {config.content_info_table}
            WHERE (imdb_id IN ({id_list}) OR program_imdb_id IN ({id_list}))
              AND content_id IS NOT NULL
        """)

    dupes = {}
    for r in rows:
        dupes.setdefault(r["imdb_id"], []).append(r)
    return dupes


def _check_policy_conflicts(content_ids, sql_client, config):
    """Check for active policy windows on content_ids."""
    if not content_ids:
        return {}
    id_list = ", ".join(f"'{escape_sql(cid)}'" for cid in content_ids)
    rows = sql_client.execute(f"""
        SELECT
            CAST(content_id AS STRING) AS content_id,
            timespan,
            country_list,
            deal_id
        FROM {config.policy_windows_table}
        WHERE CAST(content_id AS STRING) IN ({id_list})
          AND UPPER(timespan) > CAST(current_timestamp() AS STRING)
    """)
    policies = {}
    for r in rows:
        policies.setdefault(r["content_id"], []).append(r)
    return policies


def _build_display_results(requests, match_results, dupes, policies):
    """Combine match results with dupe/policy info into display rows."""
    rows = []
    for req, result in zip(requests, match_results):
        row = {
            "title": req.title,
            "type": req.content_type,
            "release_year": req.release_year,
            "content_id": result.content_id or "",
            "imdb_id": result.imdb_id or "",
            "imdb_title": result.imdb_title or "",
            "confidence": result.confidence.value,
            "match_method": result.match_method.value,
            "is_duplicate": False,
            "duplicate_content_id": "",
            "duplicate_title": "",
            "duplicate_active": "",
            "duplicate_import_id": "",
            "has_policy_conflict": False,
            "policy_details": "",
        }

        # Handle CONFLICT display
        if result.confidence == Confidence.CONFLICT and result.alternate_imdb_id:
            row["imdb_id"] = f"{result.imdb_id} vs {result.alternate_imdb_id}"

        # Dupe detection
        imdb_id = result.imdb_id
        if imdb_id and imdb_id in dupes:
            other_dupes = [d for d in dupes[imdb_id] if d["existing_content_id"] != result.content_id]
            if other_dupes:
                row["is_duplicate"] = True
                best = next((d for d in other_dupes if str(d.get("active", "")).lower() == "true"), other_dupes[0])
                row["duplicate_content_id"] = best["existing_content_id"]
                row["duplicate_title"] = best.get("existing_title", "")
                row["duplicate_active"] = str(best.get("active", "")).lower() == "true"
                row["duplicate_import_id"] = best.get("import_id", "")

                if best["existing_content_id"] in policies:
                    row["has_policy_conflict"] = True
                    windows = policies[best["existing_content_id"]]
                    row["policy_details"] = f"{len(windows)} active window(s): {windows[0].get('country_list', '')}"

        rows.append(row)
    return rows


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
            <strong>Pipeline:</strong> Verified matches → Content ID lookup → IMDB catalog match → Cross-validation → Duplicate detection → Policy conflicts<br>
            <strong>CSV format:</strong> Title, Type (MOVIE/SERIES), Year, Director (optional) — header row optional<br>
            <strong>Learning:</strong> Verified matches are remembered and auto-resolve on future runs
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
        uploaded = st.file_uploader("Or upload CSV/Excel", type=["csv", "txt", "xlsx", "xls"], key="dupe_file")
        if uploaded:
            if uploaded.name.endswith((".xlsx", ".xls")):
                input_text = _parse_excel_to_csv(uploaded)
            else:
                input_text = uploaded.read().decode("utf-8")

    if st.button("Run Dupe Check", type="primary", key="dupe_btn"):
        if not input_text or not input_text.strip():
            st.warning("Paste or upload titles first.")
            return

        requests = parse_csv_input(input_text)
        if not requests:
            st.warning("No valid titles found in input.")
            return

        progress = st.empty()
        try:
            matcher, verifier, sql_client, config = _get_engine()

            # Split: titles with pre-extracted IMDB IDs vs those needing matching
            needs_matching = []
            pre_matched = {}
            for i, req in enumerate(requests):
                known = getattr(req, "_known_imdb_id", "")
                if known:
                    pre_matched[i] = MatchResult(
                        request=req,
                        imdb_id=known,
                        confidence=Confidence.HIGH,
                        match_method=MatchMethod.DIRECT_CONTENT_INFO,
                        metadata={"source": "file_provided"},
                    )
                else:
                    needs_matching.append((i, req))

            # Run the matching engine only on titles that need it
            match_results = [None] * len(requests)
            for i, result in pre_matched.items():
                match_results[i] = result

            if needs_matching:
                unmatched_reqs = [req for _, req in needs_matching]
                progress.info(f"Matching {len(unmatched_reqs)} titles (skipping {len(pre_matched)} with known IMDB IDs)...")
                engine_results = matcher.match(unmatched_reqs, progress_callback=lambda msg: progress.info(msg))
                for j, (orig_idx, _) in enumerate(needs_matching):
                    match_results[orig_idx] = engine_results[j]
            else:
                progress.info(f"All {len(requests)} titles have IMDB IDs from file.")

            # Fill any gaps
            for i in range(len(match_results)):
                if match_results[i] is None:
                    match_results[i] = MatchResult(request=requests[i])

            # Collect IMDB IDs for dupe detection
            progress.info("Checking for duplicates...")
            imdb_ids = [r.imdb_id for r in match_results if r.imdb_id and "vs" not in r.imdb_id]
            dupes = _find_duplicates(list(set(imdb_ids)), sql_client, config)

            # Policy conflicts on duplicate content_ids
            progress.info("Checking policy conflicts...")
            dupe_cids = set()
            for dupe_list in dupes.values():
                for d in dupe_list:
                    dupe_cids.add(d["existing_content_id"])
            policies = _check_policy_conflicts(list(dupe_cids), sql_client, config)

            progress.empty()

            # Build display results
            display_rows = _build_display_results(requests, match_results, dupes, policies)
            st.session_state.dupe_results = display_rows
            st.session_state.dupe_match_results = match_results
            st.session_state.dupe_requests = requests
            st.session_state.dupe_verify_decisions = {}

        except Exception as e:
            progress.empty()
            st.error(f"Pipeline failed: {e}")
            return

    if "dupe_results" not in st.session_state or not st.session_state.dupe_results:
        return

    results = st.session_state.dupe_results
    match_results = st.session_state.get("dupe_match_results", [])
    requests = st.session_state.get("dupe_requests", [])

    # Summary metrics
    total = len(results)
    matched_imdb = len([r for r in results if r["imdb_id"] and "vs" not in r["imdb_id"]])
    verified = len([r for r in results if r["confidence"] == "VERIFIED"])
    duplicates = len([r for r in results if r["is_duplicate"]])
    conflicts = len([r for r in results if r["has_policy_conflict"]])
    needs_review = len([r for r in results if r["confidence"] in ("LOW", "MEDIUM", "CONFLICT")])

    cols = st.columns(6)
    cols[0].metric("Total", total, help="Total number of titles submitted for checking.")
    cols[1].metric("IMDB Matched", matched_imdb, help="Titles successfully matched to an IMDB ID via content_info lookup, catalog search, or file-provided ID.")
    cols[2].metric("Verified", verified, help="Matches manually confirmed by a user in the verification wizard. These are remembered and auto-resolve on future runs.")
    cols[3].metric("Duplicates", duplicates, help="Titles whose IMDB ID already exists in Tubi's backend under a different content_id — potential duplicate ingestion.")
    cols[4].metric("Conflicts", conflicts, help="Duplicates that have active policy windows (licensing deals) on the existing content_id — ingesting again may violate rights.")
    cols[5].metric("Needs Review", needs_review, help="LOW, MEDIUM, or CONFLICT confidence matches that should be manually verified before trusting the result.")

    # Verification wizard for MEDIUM/LOW/CONFLICT results
    verify_indices = [
        i for i, r in enumerate(match_results)
        if r.confidence in (Confidence.MEDIUM, Confidence.LOW, Confidence.CONFLICT)
        and r.imdb_id
        and i not in st.session_state.get("dupe_verify_decisions", {})
    ]

    if verify_indices:
        _render_verification_wizard(verify_indices, match_results, requests)

    # Results tabs
    df = pd.DataFrame(results)
    display_cols = [
        "title", "type", "release_year", "imdb_id", "confidence",
        "content_id", "is_duplicate", "duplicate_content_id",
        "duplicate_title", "has_policy_conflict", "policy_details",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    tab_all, tab_dupes, tab_review, tab_unmatched = st.tabs([
        "All Results",
        f"Duplicates ({duplicates})",
        f"Needs Review ({needs_review})",
        f"No Match ({total - matched_imdb})",
    ])

    with tab_all:
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

    with tab_dupes:
        dupe_df = df[df["is_duplicate"] == True]
        if not dupe_df.empty:
            st.dataframe(dupe_df[display_cols], use_container_width=True, hide_index=True)
        else:
            st.success("No duplicates found.")

    with tab_review:
        review_df = df[df["confidence"].isin(["LOW", "MEDIUM", "CONFLICT"])]
        if not review_df.empty:
            st.dataframe(review_df[display_cols], use_container_width=True, hide_index=True)
        else:
            st.success("All matches are HIGH confidence or VERIFIED.")

    with tab_unmatched:
        unmatched_df = df[df["imdb_id"] == ""]
        if not unmatched_df.empty:
            st.dataframe(unmatched_df[["title", "type", "release_year", "content_id"]], use_container_width=True, hide_index=True)
        else:
            st.success("All titles matched to IMDB IDs.")

    # Downloads
    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button("Download all results", df.to_csv(index=False), "imdb_dupe_check_results.csv", "text/csv")
    with col2:
        if duplicates > 0:
            st.download_button("Download duplicates", df[df["is_duplicate"] == True].to_csv(index=False), "duplicates.csv", "text/csv")
    with col3:
        if total - matched_imdb > 0:
            st.download_button("Download unmatched", df[df["imdb_id"] == ""].to_csv(index=False), "unmatched.csv", "text/csv")


def _render_verification_wizard(verify_indices, match_results, requests):
    """Render the verification wizard for MEDIUM/LOW/CONFLICT matches."""
    decisions = st.session_state.get("dupe_verify_decisions", {})
    unresolved = [i for i in verify_indices if i not in decisions]
    total_to_verify = len(verify_indices)
    completed = total_to_verify - len(unresolved)

    if not unresolved:
        st.markdown(f"""
        <div style="background:#ecfdf5; border:1px solid #a7f3d0; border-radius:12px; padding:12px 16px; margin:12px 0;">
            <span style="font-size:14px; color:#065f46; font-weight:600;">
                ✓ All {total_to_verify} verification{'s' if total_to_verify > 1 else ''} complete
            </span>
        </div>
        """, unsafe_allow_html=True)
        return

    current_idx = unresolved[0]
    result = match_results[current_idx]
    req = requests[current_idx]

    if result.confidence == Confidence.CONFLICT:
        wizard_bg = "#fef2f2"
        wizard_border = "#fecaca"
        wizard_title = "Resolve Conflict"
        wizard_prompt = f"Two paths found different IMDB IDs for <strong>{req.title}</strong>"
    else:
        wizard_bg = "#fffbeb"
        wizard_border = "#fde68a"
        wizard_title = "Verify Match"
        wizard_prompt = f"Confirm IMDB match for <strong>{req.title}</strong> ({result.confidence.value} confidence)"

    st.markdown(f"""
    <div style="background:{wizard_bg}; border:1px solid {wizard_border}; border-radius:12px; padding:16px 20px; margin:12px 0;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <div style="font-family:'Space Grotesk',sans-serif; font-size:16px; font-weight:700; color:#1b1626;">
                {wizard_title}
            </div>
            <div style="font-size:12px; color:#8a8199; font-weight:600;">
                {completed + 1} of {total_to_verify}
            </div>
        </div>
        <div style="font-size:14px; color:#4b4458; margin-bottom:8px;">
            {wizard_prompt}
        </div>
        <div style="font-size:12px; color:#8a8199;">
            Input: {req.title} | {req.content_type or 'Unknown type'} | {req.release_year or 'No year'}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Show candidates
    candidates = []
    if result.imdb_id:
        candidates.append(("Path A", result.imdb_id, result.imdb_title, result.imdb_year))
    if result.alternate_imdb_id:
        candidates.append(("Path B", result.alternate_imdb_id, None, None))

    for label, imdb_id, imdb_title, imdb_year in candidates:
        meta_str = f" — {imdb_title}" if imdb_title else ""
        year_str = f" ({imdb_year})" if imdb_year else ""
        col1, col2 = st.columns([5, 1])
        with col1:
            st.markdown(f"""
            <div style="display:flex; align-items:center; gap:12px; padding:8px 12px; background:#fff; border:1px solid #e7e3ee; border-radius:8px; margin-bottom:4px;">
                <span style="font-size:11px; font-weight:600; color:#8a8199;">{label}</span>
                <code style="font-size:14px; font-weight:600; color:#1b1626;">{imdb_id}</code>
                <span style="font-size:13px; color:#4b4458;">{meta_str}{year_str}</span>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            if st.button("Confirm", key=f"verify_{current_idx}_{imdb_id}", use_container_width=True):
                _record_user_verification(current_idx, imdb_id, result, req)
                st.rerun()

    # Manual input — button toggles between Skip and Save based on input
    col_manual, col_action = st.columns([3, 1])
    with col_manual:
        manual_id = st.text_input(
            "Or enter correct IMDB ID",
            placeholder="tt0000000",
            key=f"manual_imdb_{current_idx}",
        )
    with col_action:
        st.markdown("<div style='margin-top:26px;'></div>", unsafe_allow_html=True)
        if manual_id and manual_id.strip():
            if st.button("Save Manual ID", key=f"verify_manual_{current_idx}", use_container_width=True, type="primary"):
                clean_id = manual_id.strip()
                if re.match(r"^tt\d{7,}$", clean_id):
                    _record_user_verification(current_idx, clean_id, result, req)
                    st.rerun()
                else:
                    st.error("Format: ttXXXXXXX")
        else:
            if st.button("Skip →", key=f"verify_skip_{current_idx}", use_container_width=True):
                st.session_state.dupe_verify_decisions[current_idx] = "__skip__"
                st.rerun()


def _record_user_verification(idx, confirmed_imdb_id, result, req):
    """Record the user's verification choice."""
    _, verifier, _, _ = _get_engine()
    user_email = _get_user_email()

    record = VerificationRecord(
        title=req.title,
        imdb_id=confirmed_imdb_id,
        verified_by=user_email,
        content_type=req.content_type,
        release_year=req.release_year,
        content_id=result.content_id or "",
        imdb_title=result.imdb_title or "",
        match_method=result.match_method.value,
        verified_at=datetime.now(timezone.utc),
        import_id=req.import_id,
    )
    try:
        verifier.record_verification(record)
    except Exception:
        pass

    st.session_state.dupe_verify_decisions[idx] = confirmed_imdb_id
    # Update display results
    if "dupe_results" in st.session_state and idx < len(st.session_state.dupe_results):
        st.session_state.dupe_results[idx]["confidence"] = "VERIFIED"
        st.session_state.dupe_results[idx]["imdb_id"] = confirmed_imdb_id
