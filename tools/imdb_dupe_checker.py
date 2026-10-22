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
from tools.sony_matcher import _query_avails, _build_index, match_title, _query_import_ids

POLICY_WINDOWS = "core_prod.policydb_cdc.content_policy_windows"
RICH_CONTENT = "ml_prod.common.rich_content"
CONTENT_INFO = "core_prod.tubidw.content_info"
METRICS_TABLE = "core_dev.techops.imdb_dupe_checker_metrics"

# Suffixes that indicate channel variants of the same partner (e.g., lionsgate-original, lionsgate_linear)
_PARTNER_SUFFIXES = re.compile(r"[-_](original|linear|us|uk|latam|intl|avod|svod|fast|digital|classics)$", re.IGNORECASE)


def _partner_base(import_id):
    """Extract the base partner slug, stripping known channel suffixes."""
    if not import_id:
        return ""
    base = import_id.lower().strip()
    while True:
        stripped = _PARTNER_SUFFIXES.sub("", base)
        if stripped == base:
            break
        base = stripped
    return base


def _same_partner(id_a, id_b):
    """Check if two import_ids belong to the same partner family."""
    if not id_a or not id_b:
        return False
    return _partner_base(id_a) == _partner_base(id_b)


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
        "start_date": ["start date", "start_date", "license_start", "license start", "window start", "window_start"],
        "end_date": ["end date", "end_date", "license_end", "license end", "window end", "window_end", "expiry", "expiry date"],
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

        # Pre-extracted IMDB ID (from Excel URL parsing or raw ID)
        known_imdb = ""
        imdb_idx = col_map.get("imdb_id")
        if imdb_idx is not None and imdb_idx < len(row):
            val = row[imdb_idx].strip()
            if val.startswith("tt") and re.match(r"^tt\d{7,}$", val):
                known_imdb = val
            elif val:
                # Try extracting from URL (e.g. https://www.imdb.com/title/tt1234567/)
                known_imdb = _extract_imdb_id_from_url(val)

        # Proposed policy window dates
        start_date = ""
        start_idx = col_map.get("start_date")
        if start_idx is not None and start_idx < len(row):
            start_date = _parse_date_value(row[start_idx].strip())

        end_date = ""
        end_idx = col_map.get("end_date")
        if end_idx is not None and end_idx < len(row):
            end_date = _parse_date_value(row[end_idx].strip())

        req = MatchRequest(
            title=title,
            content_type=content_type,
            release_year=release_year,
            director=director,
        )
        # Attach known IMDB ID as metadata for the pipeline
        req._known_imdb_id = known_imdb
        # Attach proposed window if both dates present
        if start_date and end_date:
            req._proposed_window = (start_date, end_date)
        else:
            req._proposed_window = None
        results.append(req)
    return results


def _parse_date_value(val):
    """Try to parse a date string into YYYY-MM-DD format."""
    if not val:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try pandas for more flexible parsing
    try:
        return pd.to_datetime(val).strftime("%Y-%m-%d")
    except Exception:
        return ""


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
        elif lower in ("start date", "start_date", "license_start", "license start", "window start", "window_start"):
            col_renames[col] = "start_date"
        elif lower in ("end date", "end_date", "license_end", "license end", "window end", "window_end", "expiry", "expiry date"):
            col_renames[col] = "end_date"
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
    keep_cols = [c for c in ["title", "type", "year", "director", "imdb_id", "start_date", "end_date"] if c in df.columns]
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
    # Filter out trailers — they share IMDB IDs with their parent title by design
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
              AND LOWER(ci.content_type) != 'trailer'
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
              AND LOWER(content_type) != 'trailer'
        """)

    dupes = {}
    for r in rows:
        dupes.setdefault(r["imdb_id"], []).append(r)
    return dupes


def _check_policy_conflicts(content_ids, sql_client, config, proposed_windows=None):
    """Check for policy window conflicts on content_ids.

    If proposed_windows is provided (dict of content_id → (start_date, end_date)),
    checks for OVERLAP between proposed and existing windows.
    Otherwise falls back to checking if existing windows are currently active.
    """
    if not content_ids:
        return {}
    id_list = ", ".join(f"'{escape_sql(cid)}'" for cid in content_ids)

    if proposed_windows:
        # Fetch all windows (not just active today) for overlap comparison
        rows = sql_client.execute(f"""
            SELECT
                CAST(content_id AS STRING) AS content_id,
                timespan,
                country_list,
                deal_id
            FROM {config.policy_windows_table}
            WHERE CAST(content_id AS STRING) IN ({id_list})
        """)
    else:
        # Default: only windows active now
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

    if not proposed_windows:
        policies = {}
        for r in rows:
            policies.setdefault(r["content_id"], []).append(r)
        return policies

    # Window overlap detection: incoming_start < existing_end AND incoming_end > existing_start
    policies = {}
    for r in rows:
        cid = r["content_id"]
        window = proposed_windows.get(cid)
        if not window:
            continue
        proposed_start, proposed_end = window
        existing_timespan = r.get("timespan", "")
        # timespan format is typically "start/end" or just the end date
        # Parse to extract existing window bounds
        existing_start, existing_end = _parse_timespan(existing_timespan)
        if not existing_end:
            continue
        # Overlap check
        if proposed_start < existing_end and proposed_end > existing_start:
            policies.setdefault(cid, []).append(r)

    return policies


def _parse_timespan(timespan_str):
    """Parse policy timespan into (start, end) date strings.

    Timespan can be 'start/end' (ISO interval) or a single timestamp (treated as end).
    Returns (start_str, end_str) comparable with < and >.
    """
    if not timespan_str:
        return ("", "")
    if "/" in timespan_str:
        parts = timespan_str.split("/", 1)
        return (parts[0][:10], parts[1][:10])
    # Single value = end date
    return ("1900-01-01", timespan_str[:10])


def _persist_run_metrics(sql_client, config, metrics):
    """Write run-level metrics to the Delta table."""
    try:
        sql_client.execute(f"""
            CREATE TABLE IF NOT EXISTS {config.metrics_table} (
                run_date DATE,
                user_email STRING,
                import_id STRING,
                titles_submitted INT,
                file_provided INT,
                matched_auto_high INT,
                matched_auto_medium_low INT,
                verified_confirmed INT,
                verified_manual_override INT,
                verified_skipped INT,
                unmatched INT,
                duplicates_detected INT,
                conflicts_detected INT,
                auto_accuracy_pct DOUBLE,
                verification_accuracy_pct DOUBLE,
                created_at TIMESTAMP
            ) USING DELTA
        """)
    except Exception:
        pass

    m = metrics
    auto_denom = m["titles_submitted"] - m["file_provided"]
    auto_acc = round((m["matched_auto_high"] / auto_denom) * 100, 1) if auto_denom > 0 else None
    verify_denom = m["verified_confirmed"] + m["verified_manual_override"]
    verify_acc = round((m["verified_confirmed"] / verify_denom) * 100, 1) if verify_denom > 0 else None

    import_val = f"'{escape_sql(m['import_id'])}'" if m.get("import_id") else "NULL"
    auto_acc_val = str(auto_acc) if auto_acc is not None else "NULL"
    verify_acc_val = str(verify_acc) if verify_acc is not None else "NULL"

    try:
        sql_client.execute(f"""
            INSERT INTO {config.metrics_table}
            (run_date, user_email, import_id, titles_submitted, file_provided,
             matched_auto_high, matched_auto_medium_low,
             verified_confirmed, verified_manual_override, verified_skipped,
             unmatched, duplicates_detected, conflicts_detected,
             auto_accuracy_pct, verification_accuracy_pct, created_at)
            VALUES (
                current_date(),
                '{escape_sql(m["user_email"])}',
                {import_val},
                {m["titles_submitted"]},
                {m["file_provided"]},
                {m["matched_auto_high"]},
                {m["matched_auto_medium_low"]},
                {m["verified_confirmed"]},
                {m["verified_manual_override"]},
                {m["verified_skipped"]},
                {m["unmatched"]},
                {m["duplicates_detected"]},
                {m["conflicts_detected"]},
                {auto_acc_val},
                {verify_acc_val},
                current_timestamp()
            )
        """)
    except Exception:
        pass


def _build_display_results(requests, match_results, dupes, policies, selected_import_id=None, cid_import_map=None):
    """Combine match results with dupe/policy info into display rows.

    Splits backend matches into two categories:
    - In_Library: the selected partner already has this title (same import_id)
    - is_duplicate: a DIFFERENT partner has this title (different import_id)
    """
    if cid_import_map is None:
        cid_import_map = {}
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
            "in_library": False,
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
            all_matches = dupes[imdb_id]

            # Split matches: same partner vs other partners
            # Uses partner-family matching (lionsgate == lionsgate-original == lionsgate_linear)
            if selected_import_id:
                own_partner_matches = [
                    d for d in all_matches
                    if _same_partner(d.get("import_id"), selected_import_id)
                ]
                other_partner_matches = [
                    d for d in all_matches
                    if not _same_partner(d.get("import_id"), selected_import_id)
                ]
            else:
                own_partner_matches = []
                other_partner_matches = all_matches

            # Fallback: if the IMDB engine already resolved to a content_id in this dupe set,
            # treat that entry as own-partner (it's clearly already in the library)
            if not own_partner_matches and result.content_id:
                engine_match = [d for d in all_matches if str(d.get("existing_content_id")) == str(result.content_id)]
                if engine_match:
                    own_partner_matches = engine_match
                    other_partner_matches = [d for d in all_matches if str(d.get("existing_content_id")) != str(result.content_id)]

            # In_Library: partner already has this title — only populate content_id in this case
            if own_partner_matches:
                row["in_library"] = True
                best_own = next(
                    (d for d in own_partner_matches if str(d.get("active", "")).lower() == "true"),
                    own_partner_matches[0],
                )
                row["content_id"] = best_own["existing_content_id"]

            # content_id only shows when in_library is confirmed
            if not row["in_library"]:
                row["content_id"] = ""

            # Duplicate: a different partner has this title — one row per duplicate
            if other_partner_matches:
                for dupe in other_partner_matches:
                    dupe_row = dict(row)
                    dupe_row["is_duplicate"] = True
                    dupe_row["duplicate_content_id"] = dupe["existing_content_id"]
                    dupe_row["duplicate_title"] = dupe.get("existing_title", "")
                    dupe_row["duplicate_active"] = str(dupe.get("active", "")).lower() == "true"
                    dupe_row["duplicate_import_id"] = dupe.get("import_id", "")

                    if dupe["existing_content_id"] in policies:
                        dupe_row["has_policy_conflict"] = True
                        windows = policies[dupe["existing_content_id"]]
                        dupe_row["policy_details"] = f"{len(windows)} active window(s): {windows[0].get('country_list', '')}"

                    rows.append(dupe_row)
                # If title is also in_library, emit the base row too (no duplicate info)
                if row["in_library"]:
                    rows.append(row)
            else:
                rows.append(row)
            continue

        # If dupe detection didn't trigger but we have a content_id from the engine,
        # check if it belongs to the selected partner (in_library even without dupes dict hit)
        if not row["in_library"] and result.content_id and selected_import_id:
            cid_import_id = cid_import_map.get(str(result.content_id))
            if cid_import_id and _same_partner(cid_import_id, selected_import_id):
                row["in_library"] = True

        # content_id column only populates when the title is confirmed in the partner's library
        if not row["in_library"]:
            row["content_id"] = ""

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

    try:
        import_ids = _query_import_ids()
    except Exception:
        import_ids = []
    selected_import_id = st.selectbox(
        "Partner being checked (enables content ID fallback for unmatched titles)",
        ["(none)"] + import_ids,
        index=0,
        key="dupe_import_id",
        help="Select the partner whose avails list you're checking. Unmatched titles will be looked up in this partner's avails to find content IDs.",
    )
    if selected_import_id == "(none)":
        selected_import_id = None

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

            # Content ID fallback pass for unmatched titles
            if selected_import_id:
                unmatched_indices = [
                    i for i, r in enumerate(match_results)
                    if not r.imdb_id or r.imdb_id.strip() == ""
                ]
                if unmatched_indices:
                    progress.info(f"Fallback: checking {len(unmatched_indices)} unmatched titles against {selected_import_id} avails...")
                    try:
                        avails_rows = _query_avails(selected_import_id)
                        avails_index = _build_index(avails_rows)
                        fallback_cids = {}
                        for idx in unmatched_indices:
                            req = requests[idx]
                            result = match_title(req.title, req.content_type, avails_index)
                            if result["status"] in ("Match", "Fuzzy") and result["content_id"] != "NEW":
                                cid = result["content_id"].split(" / ")[0].strip()
                                fallback_cids[idx] = cid

                        if fallback_cids:
                            progress.info(f"Fallback: found {len(fallback_cids)} content IDs, looking up IMDB IDs...")
                            cid_list = ", ".join(f"'{escape_sql(cid)}'" for cid in set(fallback_cids.values()))
                            try:
                                rc_rows = sql_client.execute(f"""
                                    SELECT tubi_video_id AS content_id, imdb_id
                                    FROM {config.rich_content_table}
                                    WHERE tubi_video_id IN ({cid_list})
                                      AND imdb_id IS NOT NULL AND TRIM(imdb_id) != ''
                                """)
                            except Exception:
                                rc_rows = []
                            rc_map = {r["content_id"]: r["imdb_id"] for r in rc_rows}

                            # Also check content_info for imdb_id
                            missing_cids = [cid for cid in set(fallback_cids.values()) if cid not in rc_map]
                            if missing_cids:
                                ci_list = ", ".join(f"'{escape_sql(cid)}'" for cid in missing_cids)
                                try:
                                    ci_rows = sql_client.execute(f"""
                                        SELECT content_id, COALESCE(imdb_id, program_imdb_id) AS imdb_id
                                        FROM {config.content_info_table}
                                        WHERE content_id IN ({ci_list})
                                          AND COALESCE(imdb_id, program_imdb_id) IS NOT NULL
                                    """)
                                except Exception:
                                    ci_rows = []
                                for r in ci_rows:
                                    if r["imdb_id"] and r["imdb_id"].strip():
                                        rc_map.setdefault(r["content_id"], r["imdb_id"].strip())

                            for idx, cid in fallback_cids.items():
                                imdb_id = rc_map.get(cid)
                                match_results[idx] = MatchResult(
                                    request=requests[idx],
                                    imdb_id=imdb_id,
                                    content_id=cid,
                                    confidence=Confidence.MEDIUM if imdb_id else Confidence.LOW,
                                    match_method=MatchMethod.AVAILS_FALLBACK,
                                    metadata={"fallback_import_id": selected_import_id},
                                )
                    except Exception as e:
                        progress.warning(f"Fallback pass failed: {e}")

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

            # Build proposed_windows: map each dupe content_id to the incoming title's window
            proposed_windows = None
            has_any_window = any(getattr(req, "_proposed_window", None) for req in requests)
            if has_any_window:
                proposed_windows = {}
                # Map imdb_id → proposed window from the incoming requests
                imdb_to_window = {}
                for req, result in zip(requests, match_results):
                    window = getattr(req, "_proposed_window", None)
                    if window and result.imdb_id and "vs" not in result.imdb_id:
                        imdb_to_window[result.imdb_id] = window
                # Map each dupe content_id to the proposed window of the title that matched it
                for imdb_id, dupe_list in dupes.items():
                    window = imdb_to_window.get(imdb_id)
                    if window:
                        for d in dupe_list:
                            proposed_windows[d["existing_content_id"]] = window

            policies = _check_policy_conflicts(list(dupe_cids), sql_client, config, proposed_windows)

            # Build content_id → import_id map for engine-resolved IDs not in dupes
            cid_import_map = {}
            if selected_import_id:
                # Collect content_ids from dupes
                for dupe_list in dupes.values():
                    for d in dupe_list:
                        cid_import_map[str(d["existing_content_id"])] = d.get("import_id", "")
                # Find engine-resolved content_ids NOT already in the map
                missing_cids = [
                    str(r.content_id) for r in match_results
                    if r.content_id and str(r.content_id) not in cid_import_map
                ]
                if missing_cids:
                    cid_list = ", ".join(f"'{escape_sql(c)}'" for c in set(missing_cids))
                    try:
                        ci_rows = sql_client.execute(f"""
                            SELECT CAST(content_id AS STRING) AS content_id, import_id
                            FROM {config.content_info_table}
                            WHERE content_id IN ({cid_list})
                        """)
                        for r in ci_rows:
                            cid_import_map[str(r["content_id"])] = r.get("import_id", "")
                    except Exception:
                        pass

            progress.empty()

            # Build display results
            display_rows = _build_display_results(requests, match_results, dupes, policies, selected_import_id, cid_import_map)
            st.session_state.dupe_results = display_rows
            st.session_state.dupe_match_results = match_results
            st.session_state.dupe_requests = requests
            st.session_state.dupe_verify_decisions = {}

            # Capture run metrics baseline
            st.session_state.dupe_run_metrics = {
                "user_email": _get_user_email(),
                "import_id": selected_import_id or "",
                "titles_submitted": len(requests),
                "file_provided": len(pre_matched),
                "matched_auto_high": sum(
                    1 for r in match_results
                    if r.confidence == Confidence.HIGH and r.imdb_id
                    and r.metadata.get("source") != "file_provided"
                ),
                "matched_auto_medium_low": sum(
                    1 for r in match_results
                    if r.confidence in (Confidence.MEDIUM, Confidence.LOW, Confidence.CONFLICT)
                    and r.imdb_id
                ),
                "unmatched": sum(1 for r in match_results if not r.imdb_id),
                "duplicates_detected": len([r for r in display_rows if r["is_duplicate"]]),
                "conflicts_detected": len([r for r in display_rows if r["has_policy_conflict"]]),
                "verified_confirmed": 0,
                "verified_manual_override": 0,
                "verified_skipped": 0,
                "metrics_persisted": False,
            }

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
    in_library = len([r for r in results if r["in_library"]])
    duplicates = len([r for r in results if r["is_duplicate"]])
    conflicts = len([r for r in results if r["has_policy_conflict"]])
    needs_review = len([r for r in results if r["confidence"] in ("LOW", "MEDIUM", "CONFLICT")])

    cols = st.columns(6)
    cols[0].metric("Total", total, help="Total number of titles submitted for checking.")
    cols[1].metric("IMDB Matched", matched_imdb, help="Titles successfully matched to an IMDB ID via content_info lookup, catalog search, or file-provided ID.")
    cols[2].metric("In Library", in_library, help="Titles the selected partner already has in Tubi's backend (same import_id). No new ingestion needed.")
    cols[3].metric("Duplicates", duplicates, help="Titles that exist under a DIFFERENT partner's import_id — potential duplicate ingestion from another source.")
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
    else:
        # No verifications needed — persist metrics immediately
        _finalize_run_metrics()

    # Results tabs
    df = pd.DataFrame(results)
    display_cols = [
        "title", "type", "release_year", "imdb_id", "confidence",
        "content_id", "in_library", "is_duplicate", "duplicate_content_id",
        "duplicate_title", "duplicate_import_id", "has_policy_conflict", "policy_details",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    tab_all, tab_library, tab_dupes, tab_review, tab_unmatched = st.tabs([
        "All Results",
        f"In Library ({in_library})",
        f"Duplicates ({duplicates})",
        f"Needs Review ({needs_review})",
        f"No Match ({total - matched_imdb})",
    ])

    with tab_all:
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

    with tab_library:
        lib_df = df[df["in_library"] == True]
        if not lib_df.empty:
            st.dataframe(lib_df[display_cols], use_container_width=True, hide_index=True)
        else:
            st.info("No titles from this partner already in library.")

    with tab_dupes:
        dupe_df = df[df["is_duplicate"] == True]
        if not dupe_df.empty:
            st.dataframe(dupe_df[display_cols], use_container_width=True, hide_index=True)
        else:
            st.success("No duplicates from other partners found.")

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
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.download_button("Download all results", df.to_csv(index=False), "imdb_dupe_check_results.csv", "text/csv")
    with col2:
        if in_library > 0:
            st.download_button("Download in library", df[df["in_library"] == True].to_csv(index=False), "in_library.csv", "text/csv")
    with col3:
        if duplicates > 0:
            st.download_button("Download duplicates", df[df["is_duplicate"] == True].to_csv(index=False), "duplicates.csv", "text/csv")
    with col4:
        if needs_review > 0:
            st.download_button("Download needs review", df[df["confidence"].isin(["LOW", "MEDIUM", "CONFLICT"])].to_csv(index=False), "needs_review.csv", "text/csv")
    with col5:
        if total - matched_imdb > 0:
            st.download_button("Download unmatched", df[df["imdb_id"] == ""].to_csv(index=False), "unmatched.csv", "text/csv")

    _render_reupload_section()


def _render_reupload_section():
    """Let users re-upload CSVs with manually-researched IMDB IDs to teach the system."""
    with st.expander("Import verified IMDB IDs", expanded=False):
        st.caption(
            "Download 'Unmatched' or 'Needs Review', add the correct imdb_id column "
            "(format: tt1234567), then re-upload here. Verified IDs auto-resolve on future runs."
        )
        uploaded = st.file_uploader("Upload CSV with IMDB IDs", type=["csv"], key="reupload_verified")
        if uploaded:
            csv_text = uploaded.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(csv_text))
            rows = list(reader)

            if not rows:
                st.warning("CSV is empty.")
                return

            # Check for required columns
            cols_lower = {c.lower().strip(): c for c in rows[0].keys()} if rows else {}
            title_col = cols_lower.get("title")
            imdb_col = cols_lower.get("imdb_id")

            if not title_col or not imdb_col:
                st.error("CSV must have 'title' and 'imdb_id' columns.")
                return

            type_col = cols_lower.get("type")
            year_col = cols_lower.get("release_year") or cols_lower.get("year")

            valid_rows = []
            skipped = 0
            for row in rows:
                title = (row.get(title_col) or "").strip()
                imdb_id = (row.get(imdb_col) or "").strip()
                if not title or not re.match(r"^tt\d{7,}$", imdb_id):
                    skipped += 1
                    continue
                valid_rows.append({
                    "title": title,
                    "imdb_id": imdb_id,
                    "content_type": (row.get(type_col) or "").strip().upper() if type_col else "",
                    "release_year": (row.get(year_col) or "").strip() if year_col else "",
                })

            st.info(f"{len(valid_rows)} valid rows, {skipped} skipped (missing title or invalid IMDB ID)")

            if valid_rows and st.button("Save to verification database", type="primary", key="reupload_save_btn"):
                _, verifier, _, _ = _get_engine()
                user_email = _get_user_email()
                saved = 0
                for row in valid_rows:
                    try:
                        record = VerificationRecord(
                            title=row["title"],
                            imdb_id=row["imdb_id"],
                            verified_by=user_email,
                            content_type=row["content_type"],
                            release_year=row["release_year"],
                            match_method="manual_reupload",
                        )
                        verifier.record_verification(record)
                        saved += 1
                    except Exception:
                        pass
                st.success(f"Saved {saved} verified IMDB IDs. These will auto-resolve on future runs.")


def _finalize_run_metrics():
    """Compute final verification stats and persist metrics once per run."""
    metrics = st.session_state.get("dupe_run_metrics")
    if not metrics or metrics.get("metrics_persisted"):
        return

    # Count verification outcomes from decisions
    decisions = st.session_state.get("dupe_verify_decisions", {})
    match_results = st.session_state.get("dupe_match_results", [])
    confirmed = 0
    manual_override = 0
    skipped = 0
    for idx, decision in decisions.items():
        if decision == "__skip__":
            skipped += 1
        elif idx < len(match_results):
            proposed = match_results[idx].imdb_id
            if decision == proposed:
                confirmed += 1
            else:
                manual_override += 1

    metrics["verified_confirmed"] = confirmed
    metrics["verified_manual_override"] = manual_override
    metrics["verified_skipped"] = skipped
    metrics["metrics_persisted"] = True

    try:
        _, _, sql_client, config = _get_engine()
        _persist_run_metrics(sql_client, config, metrics)
    except Exception:
        pass


def _render_verification_wizard(verify_indices, match_results, requests):
    """Render the verification wizard for MEDIUM/LOW/CONFLICT matches."""
    decisions = st.session_state.get("dupe_verify_decisions", {})
    unresolved = [i for i in verify_indices if i not in decisions]
    total_to_verify = len(verify_indices)
    completed = total_to_verify - len(unresolved)

    if not unresolved:
        # Persist metrics once all verifications are done
        _finalize_run_metrics()

        col_done, col_back = st.columns([5, 1])
        with col_done:
            st.markdown(f"""
            <div style="background:#ecfdf5; border:1px solid #a7f3d0; border-radius:12px; padding:12px 16px; margin:12px 0;">
                <span style="font-size:14px; color:#065f46; font-weight:600;">
                    ✓ All {total_to_verify} verification{'s' if total_to_verify > 1 else ''} complete
                </span>
            </div>
            """, unsafe_allow_html=True)
        with col_back:
            if completed > 0:
                st.markdown("<div style='margin-top:12px;'></div>", unsafe_allow_html=True)
                if st.button("← Undo Last", key="verify_goback_done", use_container_width=True):
                    _undo_last_verification(verify_indices, decisions, match_results, requests)
                    st.rerun()
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
    col_manual, col_action, col_back = st.columns([3, 1, 1])
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
    with col_back:
        st.markdown("<div style='margin-top:26px;'></div>", unsafe_allow_html=True)
        if completed > 0:
            if st.button("← Back", key=f"verify_goback_{current_idx}", use_container_width=True):
                _undo_last_verification(verify_indices, decisions, match_results, requests)
                st.rerun()


def _undo_last_verification(verify_indices, decisions, match_results, requests):
    """Undo the most recent verification decision and delete from the table if it was a confirm."""
    resolved = [i for i in verify_indices if i in decisions]
    if not resolved:
        return
    last_idx = resolved[-1]
    last_decision = decisions.pop(last_idx)
    st.session_state.dupe_verify_decisions = decisions

    if last_decision != "__skip__":
        req = requests[last_idx]
        _, verifier, sql, config = _get_engine()
        from imdb_engine.normalizer import make_normalized_key
        norm_key = make_normalized_key(req.title, req.content_type, req.release_year)
        try:
            sql.execute(
                f"DELETE FROM {config.verifications_table} "
                f"WHERE normalized_key = '{escape_sql(norm_key)}' "
                f"AND imdb_id = '{escape_sql(last_decision)}'"
            )
        except Exception:
            pass

    if "dupe_results" in st.session_state and last_idx < len(st.session_state.dupe_results):
        result = match_results[last_idx]
        st.session_state.dupe_results[last_idx]["confidence"] = result.confidence.value
        st.session_state.dupe_results[last_idx]["imdb_id"] = result.imdb_id or ""


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
