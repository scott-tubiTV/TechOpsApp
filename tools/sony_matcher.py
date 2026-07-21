"""Sony Content ID Matcher — looks up Sony content IDs by title."""

import json
import os
import re
import unicodedata

import pandas as pd
import streamlit as st

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "sony_catalog.json")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_year(text: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", text).strip()


@st.cache_data
def load_catalog():
    with open(CATALOG_PATH) as f:
        rows = json.load(f)
    return rows


def build_index(catalog_rows, additions=None):
    """Build a lookup index from catalog rows + user additions.

    Returns dict: normalized_title -> list of {title, type, contentId, source}
    """
    index = {}
    for row in catalog_rows:
        key = _normalize(row["title"])
        entry = {**row, "source": "base"}
        index.setdefault(key, []).append(entry)

    if additions:
        for row in additions:
            key = _normalize(row["title"])
            entry = {**row, "source": "user"}
            index.setdefault(key, []).append(entry)

    return index


def match_title(title: str, title_type: str, index: dict) -> dict:
    """Match a single title against the index.

    Returns {input, type, contentId, status}
    """
    norm = _normalize(title)

    candidates = index.get(norm, [])
    if title_type:
        typed = [c for c in candidates if c["type"] == title_type]
        if typed:
            candidates = typed

    if candidates:
        user_entries = [c for c in candidates if c["source"] == "user"]
        if user_entries:
            latest = user_entries[-1]
            return {
                "input": title,
                "type": latest["type"],
                "contentId": latest["contentId"],
                "status": "Match (user override)",
            }
        if len(set(c["contentId"] for c in candidates)) > 1:
            ids = " / ".join(sorted(set(c["contentId"] for c in candidates)))
            return {
                "input": title,
                "type": candidates[0]["type"],
                "contentId": ids,
                "status": "Multiple matches - verify",
            }
        return {
            "input": title,
            "type": candidates[0]["type"],
            "contentId": candidates[0]["contentId"],
            "status": "Match",
        }

    stripped = _normalize(_strip_year(title))
    if stripped != norm:
        candidates = index.get(stripped, [])
        if title_type:
            typed = [c for c in candidates if c["type"] == title_type]
            if typed:
                candidates = typed

        if candidates:
            user_entries = [c for c in candidates if c["source"] == "user"]
            if user_entries:
                latest = user_entries[-1]
                return {
                    "input": title,
                    "type": latest["type"],
                    "contentId": latest["contentId"],
                    "status": "Fuzzy (user override)",
                }
            if len(set(c["contentId"] for c in candidates)) > 1:
                ids = " / ".join(sorted(set(c["contentId"] for c in candidates)))
                return {
                    "input": title,
                    "type": candidates[0]["type"],
                    "contentId": ids,
                    "status": "Multiple matches - verify",
                }
            return {
                "input": title,
                "type": candidates[0]["type"],
                "contentId": candidates[0]["contentId"],
                "status": "Fuzzy",
            }

    return {
        "input": title,
        "type": title_type or "",
        "contentId": "NEW",
        "status": "NEW",
    }


def parse_input_lines(text: str) -> list:
    """Parse input text into list of (title, type) tuples.

    Handles multi-column CSVs with quoted fields — always extracts
    column 1 as title and column 2 as type (if MOVIE/SERIES).
    """
    import csv
    import io

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


def render_sony_matcher():
    """Render the Sony Content ID Matcher tool UI in Streamlit."""
    st.markdown("""
    <div style="padding: 12px 0 4px;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.02em; color:#1b1626;">
            Sony Content ID Matcher
        </div>
        <div style="font-size:13px; color:#8a8199; margin-top:4px;">
            Upload or paste a list of titles to look up their Sony content IDs.
        </div>
    </div>
    """, unsafe_allow_html=True)

    catalog = load_catalog()

    if "sony_additions" not in st.session_state:
        st.session_state.sony_additions = []

    index = build_index(catalog, st.session_state.sony_additions)

    total = len(catalog) + len(st.session_state.sony_additions)
    st.markdown(f"""
    <div style="font-size:12px; color:#444; background:#f3f0f8; border-radius:8px; padding:8px 14px; display:inline-block; margin:8px 0 16px;">
        Catalog: {len(catalog):,} base titles + {len(st.session_state.sony_additions)} added = {total:,} total
    </div>
    """, unsafe_allow_html=True)

    tab_match, tab_add = st.tabs(["Match Titles", "Add to Catalog"])

    with tab_match:
        col_input, col_upload = st.columns([3, 1])
        with col_input:
            input_text = st.text_area(
                "Paste titles (one per line)",
                placeholder="Carmen (2023)\nLost Girl\nSome Brand New Title",
                height=150,
                key="sony_input",
            )
        with col_upload:
            uploaded = st.file_uploader("Or upload CSV/TXT", type=["csv", "txt"], key="sony_file")
            if uploaded:
                input_text = uploaded.read().decode("utf-8")

        if st.button("Match Titles", type="primary", key="sony_match_btn"):
            if input_text and input_text.strip():
                parsed = parse_input_lines(input_text)
                results = [match_title(title, ttype, index) for title, ttype in parsed]
                st.session_state.sony_results = results
            else:
                st.warning("Paste or upload titles first.")

        if "sony_results" in st.session_state and st.session_state.sony_results:
            results = st.session_state.sony_results
            df = pd.DataFrame(results)
            df.index = range(1, len(df) + 1)
            df.index.name = "#"

            counts = {
                "Match": len([r for r in results if r["status"].startswith("Match")]),
                "Fuzzy": len([r for r in results if r["status"].startswith("Fuzzy")]),
                "NEW": len([r for r in results if r["status"] == "NEW"]),
                "Multiple": len([r for r in results if "Multiple" in r["status"]]),
            }
            cols = st.columns(4)
            for col, (label, count) in zip(cols, counts.items()):
                col.metric(label, count)

            st.dataframe(
                df.style.applymap(
                    lambda v: "color: #b3261e; font-weight: 600" if v == "NEW" else "",
                    subset=["contentId"],
                ),
                use_container_width=True,
            )

            col_dl, col_copy = st.columns([1, 1])
            with col_dl:
                csv = df.to_csv()
                st.download_button("Download results (CSV)", csv, "sony_matches.csv", "text/csv")
            with col_copy:
                ids = "\n".join(r["contentId"] for r in results)
                st.code(ids, language=None)

            st.divider()
            st.markdown("**Add NEW titles to catalog:**")
            new_results = [r for r in results if r["status"] == "NEW"]
            for idx, r in enumerate(new_results):
                col_t, col_type, col_id, col_btn = st.columns([3, 1, 2, 1])
                with col_t:
                    st.text(r["input"])
                with col_type:
                    ttype = st.selectbox(
                        "Type",
                        ["MOVIE", "SERIES"],
                        key=f"new_type_{idx}",
                        label_visibility="collapsed",
                    )
                with col_id:
                    cid = st.text_input(
                        "Content ID",
                        key=f"new_cid_{idx}",
                        placeholder="e.g. 100060072",
                        label_visibility="collapsed",
                    )
                with col_btn:
                    if st.button("Add", key=f"new_add_{idx}"):
                        if cid.strip():
                            st.session_state.sony_additions.append({
                                "title": r["input"],
                                "type": ttype,
                                "contentId": cid.strip(),
                            })
                            st.success(f"Added: {r['input']}")
                            st.rerun()
                        else:
                            st.error("Enter a content ID")

    with tab_add:
        st.markdown("**Single entry:**")
        col_t, col_type, col_id, col_btn = st.columns([3, 1, 2, 1])
        with col_t:
            add_title = st.text_input("Title", key="sony_add_title", placeholder="Title")
        with col_type:
            add_type = st.selectbox("Type", ["MOVIE", "SERIES"], key="sony_add_type")
        with col_id:
            add_cid = st.text_input("Content ID", key="sony_add_cid", placeholder="100012345")
        with col_btn:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("Add", key="sony_single_add"):
                if add_title.strip() and add_cid.strip():
                    st.session_state.sony_additions.append({
                        "title": add_title.strip(),
                        "type": add_type,
                        "contentId": add_cid.strip(),
                    })
                    st.success(f"Added: {add_title.strip()}")
                    st.rerun()
                else:
                    st.error("Title and Content ID are required")

        st.divider()
        st.markdown("**Bulk add** (one per line: `Title,Type,Content ID`):")
        bulk_text = st.text_area(
            "Bulk add",
            key="sony_bulk",
            placeholder="Some Title,MOVIE,100012345\nAnother Show,SERIES,300099887",
            height=100,
            label_visibility="collapsed",
        )
        if st.button("Add all", key="sony_bulk_add"):
            added = 0
            for line in bulk_text.strip().splitlines():
                parts = line.split(",")
                if len(parts) >= 3:
                    t, tp, cid = parts[0].strip(), parts[1].strip().upper(), parts[2].strip()
                    if t and cid and tp in ("MOVIE", "SERIES"):
                        st.session_state.sony_additions.append({
                            "title": t,
                            "type": tp,
                            "contentId": cid,
                        })
                        added += 1
            if added:
                st.success(f"Added {added} titles")
                st.rerun()
            else:
                st.error("No valid entries found. Format: Title,MOVIE|SERIES,ContentID")

        st.divider()
        col_export, col_clear = st.columns(2)
        with col_export:
            full_catalog = catalog + st.session_state.sony_additions
            full_df = pd.DataFrame(full_catalog)
            csv = full_df.to_csv(index=False)
            st.download_button("Export full catalog (CSV)", csv, "sony_full_catalog.csv", "text/csv")
        with col_clear:
            if st.session_state.sony_additions:
                if st.button("Clear all added titles", type="secondary"):
                    st.session_state.sony_additions = []
                    st.rerun()

        if st.session_state.sony_additions:
            st.markdown(f"**{len(st.session_state.sony_additions)} user-added titles:**")
            add_df = pd.DataFrame(st.session_state.sony_additions)
            st.dataframe(add_df, use_container_width=True, hide_index=True)
