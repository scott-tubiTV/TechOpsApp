"""Content ID Matcher — looks up Tubi content IDs by title from the live database."""

import csv
import io
import re
import unicodedata
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

AVAIL_MOVIES = "core_prod.contentavails_cdc.avail_movies"
AVAIL_SERIES = "core_prod.contentavails_cdc.avail_series"
VERIFICATIONS_TABLE = "core_dev.techops.content_id_verifications"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_year(text: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", text).strip()


def _strip_parentheticals(text: str) -> str:
    """Strip all parenthetical suffixes: (MIRAMAX), (CBS FILMS), (2005), (S), etc."""
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def _reposition_article(norm_text: str) -> str:
    """Move trailing article to front: 'hunt for red october the' -> 'the hunt for red october'."""
    for article in ("the", "a", "an"):
        if norm_text.endswith(" " + article):
            return article + " " + norm_text[: -(len(article) + 1)]
    return norm_text


@st.cache_resource
def _get_workspace_client():
    return WorkspaceClient()


WAREHOUSE_ID = "a6b9541289d75c6e"


def _get_user_email():
    """Get current user email for verification recording."""
    try:
        user_info = st.experimental_user
        if user_info and user_info.get("email"):
            return user_info["email"]
    except Exception:
        pass
    headers = st.context.headers
    for h in ["X-Forwarded-Email", "X-Forwarded-Preferred-Username", "x-forwarded-email", "X-Databricks-User-Email"]:
        val = headers.get(h)
        if val:
            return val
    return "unknown"


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


def _get_candidate_details(content_ids):
    """Fetch title, active, release_year, language from content_info."""
    if not content_ids:
        return {}
    id_list = ", ".join(f"'{cid}'" for cid in content_ids)
    rows = _execute_sql(f"""
        SELECT content_id, title, active, release_year, language
        FROM {CONTENT_INFO}
        WHERE content_id IN ({id_list})
    """)
    return {
        r["content_id"]: {
            "title": r.get("title", ""),
            "active": str(r.get("active", "")).lower() == "true",
            "release_year": str(r.get("release_year", "") or ""),
            "language": r.get("language", "") or "",
        }
        for r in rows
    }


def _resolve_multiple_matches(results, parsed_metadata=None):
    """Post-process results: auto-resolve multiples using active status, release_year, language."""
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

    details_map = _get_candidate_details(list(all_cids))

    for i in multi_indices:
        cids = [cid.strip() for cid in results[i]["content_id"].split(" / ")]
        remaining = cids[:]

        # Filter by active status
        active_cids = [cid for cid in remaining if details_map.get(cid, {}).get("active", False)]
        if len(active_cids) == 1:
            results[i]["content_id"] = active_cids[0]
            results[i]["status"] = "Match"
            continue
        if active_cids:
            remaining = active_cids

        # If CSV provided release_year, narrow further
        meta = parsed_metadata[i] if parsed_metadata else None
        if meta and meta.get("release_year") and len(remaining) > 1:
            input_year = meta["release_year"]
            year_matches = [
                cid for cid in remaining
                if details_map.get(cid, {}).get("release_year") == input_year
            ]
            if len(year_matches) == 1:
                results[i]["content_id"] = year_matches[0]
                results[i]["status"] = "Match"
                continue
            if year_matches:
                remaining = year_matches

        # If CSV provided language, narrow further
        if meta and meta.get("language") and len(remaining) > 1:
            input_lang = meta["language"].lower()
            lang_matches = [
                cid for cid in remaining
                if input_lang in details_map.get(cid, {}).get("language", "").lower()
            ]
            if len(lang_matches) == 1:
                results[i]["content_id"] = lang_matches[0]
                results[i]["status"] = "Match"
                continue
            if lang_matches:
                remaining = lang_matches

        # If narrowed to one, resolve
        if len(remaining) == 1:
            results[i]["content_id"] = remaining[0]
            results[i]["status"] = "Match"
        elif remaining != cids:
            results[i]["content_id"] = " / ".join(sorted(set(remaining)))

    return results


def _record_verification(title, import_id, selected_content_id, candidate_ids, selected_by):
    """Write a verification record to the Delta table."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    candidates_str = ",".join(candidate_ids)
    _execute_sql(f"""
        INSERT INTO {VERIFICATIONS_TABLE}
        (title, import_id, selected_content_id, candidate_ids, selected_by, selected_at)
        VALUES ('{title.replace("'", "''")}', '{import_id}', '{selected_content_id}',
                '{candidates_str}', '{selected_by}', '{ts}')
    """)


@st.cache_data(ttl=3600)
def _load_prior_verifications(import_id):
    """Load previously verified title→content_id mappings for an import_id."""
    rows = _execute_sql(f"""
        SELECT title, selected_content_id
        FROM {VERIFICATIONS_TABLE}
        WHERE import_id = '{import_id}'
    """)
    lookup = {}
    for r in rows:
        lookup[_normalize(r["title"])] = r["selected_content_id"]
    return lookup


def _apply_prior_verifications(results, import_id):
    """Auto-resolve fuzzy/multiple matches using prior user verifications."""
    prior = _load_prior_verifications(import_id)
    if not prior:
        return results
    for r in results:
        if r["status"] not in ("Fuzzy", "Multiple matches - verify"):
            continue
        norm_title = _normalize(r["input"])
        if norm_title in prior:
            r["content_id"] = prior[norm_title]
            r["status"] = "Match"
    return results


def _build_index(rows):
    """Build normalized title -> list of records index.

    Also indexes article-repositioned and parenthetical-stripped forms.
    """
    index = {}
    for row in rows:
        if not row.get("title"):
            continue
        key = _normalize(row["title"])
        index.setdefault(key, []).append(row)
        repo = _reposition_article(key)
        if repo != key:
            index.setdefault(repo, []).append(row)
        stripped = _normalize(_strip_parentheticals(row["title"]))
        if stripped != key:
            index.setdefault(stripped, []).append(row)
            repo_stripped = _reposition_article(stripped)
            if repo_stripped != stripped:
                index.setdefault(repo_stripped, []).append(row)
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

    # Try article repositioning: "title the" -> "the title"
    repositioned = _reposition_article(norm)
    if repositioned != norm:
        candidates = index.get(repositioned, [])
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

    # Try both: strip year then reposition article
    if stripped != norm:
        repo_stripped = _reposition_article(stripped)
        if repo_stripped != stripped:
            candidates = index.get(repo_stripped, [])
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

    # Strip all parentheticals and try with article reposition combos
    no_parens = _normalize(_strip_parentheticals(title))
    if no_parens != norm:
        for variant in (no_parens, _reposition_article(no_parens)):
            if variant == norm:
                continue
            candidates = index.get(variant, [])
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
    """Parse input text into list of dicts with title, type, release_year, language.

    Handles multi-column CSVs with quoted fields. Detects columns by header name
    when present; falls back to positional (col 1 = title, col 2 = type).
    """
    results = []
    reader = csv.reader(io.StringIO(text.strip()))
    rows_list = list(reader)
    if not rows_list:
        return results

    # Try to detect header row and map columns
    col_map = {}
    first_row = [c.strip().lower() for c in rows_list[0]]
    header_keywords = {
        "title": ["title"],
        "type": ["type", "prod type", "content type"],
        "release_year": ["release year", "release_year", "year"],
        "language": ["original language", "language", "original_language"],
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
        elif len(row) > 1 and "type" not in col_map:
            val = row[1].strip().upper()
            if val in ("MOVIE", "SERIES"):
                title_type = val

        release_year = ""
        year_idx = col_map.get("release_year")
        if year_idx is not None and year_idx < len(row):
            val = row[year_idx].strip()
            if re.match(r"^\d{4}$", val):
                release_year = val

        language = ""
        lang_idx = col_map.get("language")
        if lang_idx is not None and lang_idx < len(row):
            language = row[lang_idx].strip()

        results.append({
            "title": title,
            "type": title_type,
            "release_year": release_year,
            "language": language,
        })
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
                results = [match_title(p["title"], p["type"], index) for p in parsed]
                results = _resolve_multiple_matches(results, parsed)
                results = _apply_prior_verifications(results, selected_import_id)
                st.session_state.matcher_results = results
                st.session_state.verify_index = 0
                st.session_state.verify_decisions = {}
                st.session_state.candidate_details = {}
            except Exception as e:
                st.error(f"Failed to load content database: {e}")
        else:
            st.warning("Paste or upload titles first.")

    if "matcher_results" in st.session_state and st.session_state.matcher_results:
        results = st.session_state.matcher_results

        # Initialize verification state
        if "verify_index" not in st.session_state:
            st.session_state.verify_index = 0
        if "verify_decisions" not in st.session_state:
            st.session_state.verify_decisions = {}
        if "candidate_details" not in st.session_state:
            st.session_state.candidate_details = {}

        # Find indices needing verification (multiples + fuzzy)
        verify_indices = [
            i for i, r in enumerate(results)
            if r["status"] in ("Multiple matches - verify", "Fuzzy")
        ]

        # Load candidate details once for all content_ids needing verification
        if verify_indices and not st.session_state.candidate_details:
            all_cids = set()
            for i in verify_indices:
                for cid in results[i]["content_id"].split(" / "):
                    all_cids.add(cid.strip())
            try:
                st.session_state.candidate_details = _get_candidate_details(list(all_cids))
            except Exception:
                st.session_state.candidate_details = {}

        # Apply decisions to results for display/export
        display_results = []
        for i, r in enumerate(results):
            row = r.copy()
            if i in st.session_state.verify_decisions:
                decision = st.session_state.verify_decisions[i]
                if decision == "__skip__":
                    if r["status"] == "Multiple matches - verify":
                        row["status"] = "Multiple matches - skipped"
                    else:
                        row["status"] = "Fuzzy - skipped"
                elif decision == "__new__":
                    row["content_id"] = "NEW"
                    row["status"] = "NEW"
                elif decision == "__confirm__":
                    row["status"] = "Match"
                else:
                    row["content_id"] = decision
                    row["status"] = "Match"
            display_results.append(row)

        # Verification wizard
        unresolved = [
            idx for idx in verify_indices
            if idx not in st.session_state.verify_decisions
        ]

        if verify_indices:
            total_to_verify = len(verify_indices)
            completed = total_to_verify - len(unresolved)

            if unresolved:
                current_pos = st.session_state.verify_index
                if current_pos >= len(unresolved):
                    current_pos = len(unresolved) - 1
                    st.session_state.verify_index = current_pos

                current_result_idx = unresolved[current_pos]
                current_result = results[current_result_idx]
                is_fuzzy = current_result["status"] == "Fuzzy"
                cids = [cid.strip() for cid in current_result["content_id"].split(" / ")]
                details = st.session_state.candidate_details

                if is_fuzzy:
                    wizard_title = "Confirm Fuzzy Match"
                    wizard_prompt = f"Is <strong>{cids[0]}</strong> the correct match for <strong>{current_result['input']}</strong>?"
                    wizard_bg = "#fffbeb"
                    wizard_border = "#fde68a"
                else:
                    wizard_title = "Verify Match"
                    wizard_prompt = f"Which content_id is correct for <strong>{current_result['input']}</strong>?"
                    wizard_bg = "#faf5ff"
                    wizard_border = "#e9d5ff"

                st.markdown(f"""
                <div style="background:{wizard_bg}; border:1px solid {wizard_border}; border-radius:12px; padding:16px 20px; margin:12px 0;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                        <div style="font-family:'Space Grotesk',sans-serif; font-size:16px; font-weight:700; color:#1b1626;">
                            {wizard_title}
                        </div>
                        <div style="font-size:12px; color:#8a8199; font-weight:600;">
                            {completed + current_pos + 1} of {total_to_verify}
                        </div>
                    </div>
                    <div style="font-size:14px; color:#4b4458; margin-bottom:4px;">
                        {wizard_prompt}
                    </div>
                </div>
                """, unsafe_allow_html=True)

                for cid in cids:
                    info = details.get(cid, {})
                    ci_title = info.get("title", "Unknown")
                    active = info.get("active", False)
                    active_color = "#059669" if active else "#b3261e"
                    active_label = "Active" if active else "Inactive"
                    year_str = info.get("release_year", "")
                    lang_str = info.get("language", "")
                    meta_parts = []
                    if year_str:
                        meta_parts.append(year_str)
                    if lang_str:
                        meta_parts.append(lang_str)
                    meta_display = f' <span style="font-size:11px; color:#8a8199;">({", ".join(meta_parts)})</span>' if meta_parts else ""

                    row_col1, row_col2 = st.columns([5, 1])
                    with row_col1:
                        st.markdown(f"""
                        <div style="display:flex; align-items:center; gap:12px; padding:8px 12px; background:#fff; border:1px solid #e7e3ee; border-radius:8px; margin-bottom:4px;">
                            <code style="font-size:14px; font-weight:600; color:#1b1626; user-select:all; cursor:text;">{cid}</code>
                            <span style="font-size:13px; color:#4b4458;">— {ci_title}{meta_display}</span>
                            <span style="font-size:12px; font-weight:600; color:{active_color};">{active_label}</span>
                        </div>
                        """, unsafe_allow_html=True)
                    with row_col2:
                        if is_fuzzy:
                            if st.button(
                                "Confirm",
                                key=f"verify_select_{current_result_idx}_{cid}",
                                use_container_width=True,
                            ):
                                st.session_state.verify_decisions[current_result_idx] = "__confirm__"
                                try:
                                    user_email = _get_user_email()
                                    _record_verification(
                                        current_result["input"],
                                        current_result.get("import_id", ""),
                                        cid,
                                        cids,
                                        user_email,
                                    )
                                except Exception:
                                    pass
                                if current_pos + 1 < len(unresolved):
                                    st.session_state.verify_index = current_pos + 1
                                else:
                                    st.session_state.verify_index = 0
                                st.rerun()
                        else:
                            if st.button(
                                "Select",
                                key=f"verify_select_{current_result_idx}_{cid}",
                                use_container_width=True,
                            ):
                                st.session_state.verify_decisions[current_result_idx] = cid
                                try:
                                    user_email = _get_user_email()
                                    _record_verification(
                                        current_result["input"],
                                        current_result.get("import_id", ""),
                                        cid,
                                        cids,
                                        user_email,
                                    )
                                except Exception:
                                    pass
                                if current_pos + 1 < len(unresolved):
                                    st.session_state.verify_index = current_pos + 1
                                else:
                                    st.session_state.verify_index = 0
                                st.rerun()

                nav_col1, nav_col2, nav_col3, nav_col4 = st.columns([1, 1, 1, 3])
                with nav_col1:
                    if st.button("← Back", key="verify_back", disabled=(current_pos == 0 and completed == 0)):
                        if current_pos > 0:
                            st.session_state.verify_index = current_pos - 1
                        elif completed > 0:
                            decided_indices = sorted(
                                idx for idx in verify_indices
                                if idx in st.session_state.verify_decisions
                            )
                            if decided_indices:
                                del st.session_state.verify_decisions[decided_indices[-1]]
                                st.session_state.verify_index = 0
                        st.rerun()
                with nav_col2:
                    if st.button("Skip →", key="verify_skip"):
                        st.session_state.verify_decisions[current_result_idx] = "__skip__"
                        if current_pos + 1 < len(unresolved):
                            st.session_state.verify_index = current_pos + 1
                        else:
                            st.session_state.verify_index = 0
                        st.rerun()
                with nav_col3:
                    if is_fuzzy:
                        if st.button("Mark NEW", key="verify_new"):
                            st.session_state.verify_decisions[current_result_idx] = "__new__"
                            if current_pos + 1 < len(unresolved):
                                st.session_state.verify_index = current_pos + 1
                            else:
                                st.session_state.verify_index = 0
                            st.rerun()
            else:
                st.markdown(f"""
                <div style="background:#ecfdf5; border:1px solid #a7f3d0; border-radius:12px; padding:12px 16px; margin:12px 0;">
                    <span style="font-size:14px; color:#065f46; font-weight:600;">
                        ✓ All {total_to_verify} verification{'s' if total_to_verify > 1 else ''} complete
                    </span>
                </div>
                """, unsafe_allow_html=True)

        # Results table (reflects live decisions)
        df = pd.DataFrame(display_results)
        df.index = range(1, len(df) + 1)
        df.index.name = "#"

        counts = {
            "Match": len([r for r in display_results if r["status"] == "Match"]),
            "Fuzzy": len([r for r in display_results if r["status"] == "Fuzzy"]),
            "NEW": len([r for r in display_results if r["status"] == "NEW"]),
            "Multiple": len([r for r in display_results if "Multiple" in r["status"]]),
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

        matched = [r for r in display_results if r["status"] == "Match"]
        new_only = [r for r in display_results if r["status"] == "NEW"]

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
