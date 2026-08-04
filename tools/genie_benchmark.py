"""Genie Benchmark — admin tool to validate Genie space accuracy against known-correct SQL."""

import time
import uuid
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient


WAREHOUSE_ID = "a6b9541289d75c6e"
BENCHMARK_TABLE = "core_dev.techops.genie_benchmarks"

GENIE_SPACES = {
    "Content Metrics": {"id": "01f18b97de0117d8af61d23c1eaa8d7e"},
    "Redeliveries": {"id": "01f18b97de2f122eb27a1b307d68e50e"},
    "Dupe Checker V2": {"id": "01f122f4e2921b7a9c28ef03d0812ee6"},
}


@st.cache_resource
def _get_workspace_client():
    return WorkspaceClient()


def _execute_sql(query):
    """Execute a SQL query via the Statements API and return rows as list of dicts."""
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


def _ensure_benchmark_table():
    """Verify the benchmarks Delta table exists (created externally with proper permissions)."""
    _execute_sql(f"SELECT 1 FROM {BENCHMARK_TABLE} LIMIT 1")


def _load_benchmarks():
    """Load all benchmark rows from the table."""
    try:
        rows = _execute_sql(f"SELECT * FROM {BENCHMARK_TABLE} ORDER BY space_name, created_at")
        return rows
    except Exception:
        return []


def _insert_benchmark(space_name, question, expected_table, validation_sql, tolerance_type, tolerance_value):
    """Insert a new benchmark test case."""
    benchmark_id = str(uuid.uuid4())
    space_id = GENIE_SPACES.get(space_name, {}).get("id", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    escaped_question = question.replace("'", "''")
    escaped_sql = validation_sql.replace("'", "''")
    escaped_table = expected_table.replace("'", "''")

    _execute_sql(f"""
        INSERT INTO {BENCHMARK_TABLE}
        (benchmark_id, space_id, space_name, question, expected_table, validation_sql,
         tolerance_type, tolerance_value, is_active, created_at, updated_at)
        VALUES (
            '{benchmark_id}', '{space_id}', '{space_name}', '{escaped_question}',
            '{escaped_table}', '{escaped_sql}', '{tolerance_type}', {tolerance_value},
            true, '{now}', '{now}'
        )
    """)


def _update_benchmark(benchmark_id, space_name, question, expected_table, validation_sql, tolerance_type, tolerance_value, is_active):
    """Update an existing benchmark test case."""
    space_id = GENIE_SPACES.get(space_name, {}).get("id", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    escaped_question = question.replace("'", "''")
    escaped_sql = validation_sql.replace("'", "''")
    escaped_table = expected_table.replace("'", "''")
    escaped_id = benchmark_id.replace("'", "''")

    _execute_sql(f"""
        UPDATE {BENCHMARK_TABLE}
        SET space_id = '{space_id}',
            space_name = '{space_name}',
            question = '{escaped_question}',
            expected_table = '{escaped_table}',
            validation_sql = '{escaped_sql}',
            tolerance_type = '{tolerance_type}',
            tolerance_value = {tolerance_value},
            is_active = {str(is_active).lower()},
            updated_at = '{now}'
        WHERE benchmark_id = '{escaped_id}'
    """)


def _delete_benchmark(benchmark_id):
    """Delete a benchmark test case."""
    escaped_id = benchmark_id.replace("'", "''")
    _execute_sql(f"DELETE FROM {BENCHMARK_TABLE} WHERE benchmark_id = '{escaped_id}'")


def _seed_benchmarks():
    """Pre-seed the benchmark table with initial test cases if it is empty."""
    existing = _load_benchmarks()
    if existing:
        return

    seeds = [
        {
            "space_name": "Content Metrics",
            "question": "How many new avails were created this month?",
            "expected_table": "avails_status_snapshot",
            "validation_sql": "SELECT COUNT(*) as answer FROM core_dev.techops.avails_status_snapshot WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.avails_status_snapshot) AND inserted_at >= date_trunc('month', current_date())",
            "tolerance_type": "contains",
            "tolerance_value": 0.0,
        },
        {
            "space_name": "Content Metrics",
            "question": "How many titles were imported last month by content type?",
            "expected_table": "imported_titles_monthly",
            "validation_sql": "SELECT SUM(count) as answer FROM core_dev.techops.imported_titles_monthly WHERE month = date_trunc('month', dateadd(MONTH, -1, current_date()))",
            "tolerance_type": "percentage",
            "tolerance_value": 0.05,
        },
        {
            "space_name": "Content Metrics",
            "question": "How many titles did contractors review this month?",
            "expected_table": "qa_abf_summary",
            "validation_sql": "SELECT SUM(count) as answer FROM core_dev.techops.qa_abf_summary WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.qa_abf_summary) AND metric_type = 'monthly_review' AND status = 'DONE' AND team = 'Contractor' AND month = date_trunc('month', current_date())",
            "tolerance_type": "exact",
            "tolerance_value": 0.0,
        },
        {
            "space_name": "Redeliveries",
            "question": "How many open redeliveries are there right now by asset type?",
            "expected_table": "redeliveries",
            "validation_sql": "SELECT COUNT(*) as answer FROM core_dev.techops.redeliveries WHERE dismissed = false AND is_active_redelivery = true",
            "tolerance_type": "percentage",
            "tolerance_value": 0.01,
        },
        {
            "space_name": "Content Metrics",
            "question": "How many active content partners do we have?",
            "expected_table": "partner_summary",
            "validation_sql": "SELECT COUNT(*) as answer FROM core_dev.techops.partner_summary WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.partner_summary) AND partner_status = 'Active'",
            "tolerance_type": "exact",
            "tolerance_value": 0.0,
        },
        {
            "space_name": "Content Metrics",
            "question": "How many titles are currently live?",
            "expected_table": "policy_window_snapshots",
            "validation_sql": "SELECT COUNT(DISTINCT content_id) as answer FROM core_dev.techops.policy_window_snapshots WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM core_dev.techops.policy_window_snapshots) AND window_start <= current_date() AND window_end >= current_date() AND policy_disabled = false",
            "tolerance_type": "contains",
            "tolerance_value": 0.0,
        },
    ]

    for seed in seeds:
        _insert_benchmark(**seed)


def _ask_genie(space_id, question):
    """Send a question to the Genie API and poll for the result.

    Returns a dict with keys: status, generated_sql, error
    """
    w = _get_workspace_client()

    try:
        resp = w.api_client.do(
            "POST",
            f"/api/2.0/genie/spaces/{space_id}/start-conversation",
            body={"content": question},
        )
    except Exception as e:
        return {"status": "ERROR", "generated_sql": None, "error": str(e)}

    conversation_id = resp.get("conversation_id")
    message_id = resp.get("message_id")

    if not conversation_id or not message_id:
        return {"status": "ERROR", "generated_sql": None, "error": "No conversation_id/message_id returned"}

    # Poll for completion (up to ~60 seconds)
    for _ in range(30):
        time.sleep(2)
        try:
            result = w.api_client.do(
                "GET",
                f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}",
            )
        except Exception as e:
            return {"status": "ERROR", "generated_sql": None, "error": f"Poll failed: {e}"}

        status = result.get("status")
        if status == "COMPLETED":
            # Extract SQL from attachments
            attachments = result.get("attachments", [])
            generated_sql = None
            for att in attachments:
                query_obj = att.get("query")
                if query_obj:
                    generated_sql = query_obj.get("query")
                    if generated_sql:
                        break
            return {"status": "COMPLETED", "generated_sql": generated_sql, "error": None}
        elif status == "FAILED":
            error_msg = result.get("error", {}).get("message", "Genie returned FAILED")
            return {"status": "FAILED", "generated_sql": None, "error": error_msg}

    return {"status": "TIMEOUT", "generated_sql": None, "error": "Timed out waiting for Genie response"}


def _run_validation_sql(validation_sql):
    """Run validation SQL and return the first-row, first-column value as a float (or None)."""
    try:
        rows = _execute_sql(validation_sql)
        if rows:
            first_val = list(rows[0].values())[0]
            return float(first_val) if first_val is not None else None
    except Exception as e:
        return None


def _run_generated_sql(generated_sql):
    """Run the Genie-generated SQL and return the first-row, first-column value as a float (or None)."""
    try:
        rows = _execute_sql(generated_sql)
        if rows:
            first_val = list(rows[0].values())[0]
            return float(first_val) if first_val is not None else None
    except Exception:
        return None


def _compare_results(genie_answer, expected_answer, generated_sql, expected_table, tolerance_type, tolerance_value):
    """Compare Genie output against expected based on tolerance_type.

    Returns a dict: {status: "PASS"|"FAIL"|"ERROR", detail: str}
    """
    if tolerance_type == "contains":
        if generated_sql and expected_table.lower() in generated_sql.lower():
            return {"status": "PASS", "detail": f"Table '{expected_table}' found in generated SQL"}
        else:
            return {"status": "FAIL", "detail": f"Table '{expected_table}' NOT found in generated SQL"}

    if tolerance_type == "exact":
        if genie_answer is None or expected_answer is None:
            return {"status": "ERROR", "detail": "Could not extract numeric answer from one or both queries"}
        if genie_answer == expected_answer:
            return {"status": "PASS", "detail": f"Exact match: {genie_answer}"}
        else:
            return {"status": "FAIL", "detail": f"Genie={genie_answer}, Expected={expected_answer}"}

    if tolerance_type == "percentage":
        if genie_answer is None or expected_answer is None:
            return {"status": "ERROR", "detail": "Could not extract numeric answer from one or both queries"}
        if expected_answer == 0:
            if genie_answer == 0:
                return {"status": "PASS", "detail": "Both are zero"}
            else:
                return {"status": "FAIL", "detail": f"Expected 0, got {genie_answer}"}
        pct_diff = abs(genie_answer - expected_answer) / abs(expected_answer)
        if pct_diff <= tolerance_value:
            return {"status": "PASS", "detail": f"Within {tolerance_value*100:.1f}% tolerance (diff={pct_diff*100:.2f}%)"}
        else:
            return {"status": "FAIL", "detail": f"Exceeds {tolerance_value*100:.1f}% tolerance (diff={pct_diff*100:.2f}%, Genie={genie_answer}, Expected={expected_answer})"}

    return {"status": "ERROR", "detail": f"Unknown tolerance_type: {tolerance_type}"}


def _run_all_benchmarks(benchmarks):
    """Run all active benchmarks and return results list."""
    active = [b for b in benchmarks if str(b.get("is_active", "")).lower() == "true"]
    results = []
    total = len(active)

    progress_bar = st.progress(0, text="Starting benchmarks...")
    status_text = st.empty()

    for idx, bench in enumerate(active):
        space_name = bench.get("space_name", "Unknown")
        question = bench.get("question", "")
        space_id = bench.get("space_id", "")
        expected_table = bench.get("expected_table", "")
        validation_sql = bench.get("validation_sql", "")
        tolerance_type = bench.get("tolerance_type", "exact")
        tolerance_value = float(bench.get("tolerance_value", 0) or 0)

        progress_bar.progress((idx) / total, text=f"Running {idx + 1}/{total}: {question[:50]}...")
        status_text.caption(f"Asking Genie space '{space_name}'...")

        # Step 1: Ask Genie
        genie_result = _ask_genie(space_id, question)

        generated_sql = genie_result.get("generated_sql")
        genie_answer = None
        expected_answer = None

        if genie_result["status"] == "COMPLETED" and generated_sql:
            # Step 2: Run generated SQL to get Genie's answer
            status_text.caption(f"Running Genie's SQL...")
            genie_answer = _run_generated_sql(generated_sql)

            # Step 3: Run validation SQL to get expected answer
            status_text.caption(f"Running validation SQL...")
            expected_answer = _run_validation_sql(validation_sql)

            # Step 4: Compare
            comparison = _compare_results(
                genie_answer, expected_answer, generated_sql,
                expected_table, tolerance_type, tolerance_value,
            )
        elif genie_result["status"] == "COMPLETED" and not generated_sql:
            comparison = {"status": "ERROR", "detail": "Genie completed but produced no SQL"}
        else:
            comparison = {"status": "ERROR", "detail": genie_result.get("error", "Unknown Genie error")}

        results.append({
            "question": question,
            "space": space_name,
            "status": comparison["status"],
            "genie_answer": genie_answer,
            "expected_answer": expected_answer,
            "generated_sql": generated_sql or "",
            "detail": comparison["detail"],
        })

    progress_bar.progress(1.0, text="Complete!")
    status_text.empty()
    return results


def render_genie_benchmark():
    """Render the Genie Benchmark admin tool UI."""
    if st.button("← Back to Chat", key="back_from_benchmark"):
        st.session_state.active_tool = None
        st.rerun()

    st.markdown("""
    <div style="padding: 12px 0 4px;">
        <div style="font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.02em; color:#1b1626;">
            Genie Benchmark
        </div>
        <div style="font-size:13px; color:#8a8199; margin-top:4px;">
            Validate Genie space accuracy by running test questions against known-correct SQL.
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Ensure table exists and seed if needed
    try:
        _ensure_benchmark_table()
        _seed_benchmarks()
    except Exception as e:
        st.error(f"Failed to initialize benchmark table: {e}")
        return

    # Load benchmarks
    benchmarks = _load_benchmarks()
    active_count = len([b for b in benchmarks if str(b.get("is_active", "")).lower() == "true"])

    # --- Results Summary (if we have run results in session) ---
    if "benchmark_results" in st.session_state and st.session_state.benchmark_results:
        results = st.session_state.benchmark_results
        passed = len([r for r in results if r["status"] == "PASS"])
        failed = len([r for r in results if r["status"] == "FAIL"])
        errors = len([r for r in results if r["status"] == "ERROR"])
        total = len(results)

        if passed == total:
            summary_color = "#059669"
            summary_bg = "#ecfdf5"
            summary_border = "#a7f3d0"
        elif failed > 0:
            summary_color = "#b3261e"
            summary_bg = "#fef2f2"
            summary_border = "#fecaca"
        else:
            summary_color = "#d97706"
            summary_bg = "#fffbeb"
            summary_border = "#fde68a"

        st.markdown(f"""
        <div style="background:{summary_bg}; border:1px solid {summary_border}; border-radius:12px; padding:16px 20px; margin:16px 0;">
            <div style="font-family:'Space Grotesk',sans-serif; font-size:18px; font-weight:700; color:{summary_color};">
                {passed}/{total} Passed
            </div>
            <div style="font-size:13px; color:#4b4458; margin-top:4px;">
                {passed} passed, {failed} failed, {errors} errors
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Results table
        results_df = pd.DataFrame([
            {
                "Question": r["question"],
                "Space": r["space"],
                "Status": r["status"],
                "Genie Answer": r["genie_answer"] if r["genie_answer"] is not None else "-",
                "Expected Answer": r["expected_answer"] if r["expected_answer"] is not None else "-",
                "Detail": r["detail"],
            }
            for r in results
        ])

        def _color_status(val):
            if val == "PASS":
                return "background-color: #dcfce7; color: #166534; font-weight: 600;"
            elif val == "FAIL":
                return "background-color: #fee2e2; color: #991b1b; font-weight: 600;"
            else:
                return "background-color: #fef3c7; color: #92400e; font-weight: 600;"

        styled_df = results_df.style.applymap(_color_status, subset=["Status"])
        st.dataframe(styled_df, use_container_width=True, hide_index=True)

        # Expandable generated SQL for each result
        for r in results:
            if r["generated_sql"]:
                with st.expander(f"SQL for: {r['question'][:60]}..."):
                    st.code(r["generated_sql"], language="sql")

        st.divider()

    # --- Run Benchmarks ---
    col_run, col_count = st.columns([2, 4])
    with col_run:
        run_btn = st.button("Run All Benchmarks", type="primary", key="run_benchmarks")
    with col_count:
        st.markdown(f"""
        <div style="display:flex; align-items:center; height:100%; padding-top:8px;">
            <span style="font-size:13px; color:#8a8199;">{active_count} active test cases</span>
        </div>
        """, unsafe_allow_html=True)

    if run_btn:
        if active_count == 0:
            st.warning("No active benchmarks to run. Add some test cases below.")
        else:
            results = _run_all_benchmarks(benchmarks)
            st.session_state.benchmark_results = results
            st.rerun()

    st.divider()

    # --- Add/Edit Benchmark Form ---
    st.markdown("""
    <div style="font-size:10.5px; font-weight:700; letter-spacing:0.13em; color:#9990a8; text-transform:uppercase; padding:8px 0;">
        Manage Test Cases
    </div>
    """, unsafe_allow_html=True)

    # Existing benchmarks list
    if benchmarks:
        for bench in benchmarks:
            is_active = str(bench.get("is_active", "")).lower() == "true"
            active_badge = "Active" if is_active else "Inactive"
            active_color = "#059669" if is_active else "#b3261e"
            space = bench.get("space_name", "Unknown")
            question = bench.get("question", "")
            tolerance = bench.get("tolerance_type", "")
            bench_id = bench.get("benchmark_id", "")

            with st.expander(f"{'[Active]' if is_active else '[Inactive]'} {space} — {question[:60]}"):
                with st.form(key=f"edit_{bench_id}"):
                    edit_space = st.selectbox(
                        "Space",
                        list(GENIE_SPACES.keys()),
                        index=list(GENIE_SPACES.keys()).index(space) if space in GENIE_SPACES else 0,
                        key=f"space_{bench_id}",
                    )
                    edit_question = st.text_input("Question", value=question, key=f"q_{bench_id}")
                    edit_table = st.text_input("Expected Table", value=bench.get("expected_table", ""), key=f"tbl_{bench_id}")
                    edit_sql = st.text_area("Validation SQL", value=bench.get("validation_sql", ""), height=100, key=f"sql_{bench_id}")
                    col_tol_type, col_tol_val = st.columns(2)
                    with col_tol_type:
                        tol_types = ["exact", "percentage", "contains"]
                        edit_tol_type = st.selectbox(
                            "Tolerance Type",
                            tol_types,
                            index=tol_types.index(tolerance) if tolerance in tol_types else 0,
                            key=f"toltype_{bench_id}",
                        )
                    with col_tol_val:
                        edit_tol_val = st.number_input(
                            "Tolerance Value",
                            value=float(bench.get("tolerance_value", 0) or 0),
                            min_value=0.0,
                            step=0.01,
                            key=f"tolval_{bench_id}",
                        )
                    edit_active = st.checkbox("Active", value=is_active, key=f"active_{bench_id}")

                    col_save, col_delete = st.columns([1, 1])
                    with col_save:
                        if st.form_submit_button("Save Changes"):
                            try:
                                _update_benchmark(
                                    bench_id, edit_space, edit_question, edit_table,
                                    edit_sql, edit_tol_type, edit_tol_val, edit_active,
                                )
                                st.toast("Benchmark updated!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to update: {e}")
                    with col_delete:
                        if st.form_submit_button("Delete", type="secondary"):
                            try:
                                _delete_benchmark(bench_id)
                                st.toast("Benchmark deleted!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to delete: {e}")

    st.divider()

    # Add new benchmark form
    st.markdown("""
    <div style="font-size:10.5px; font-weight:700; letter-spacing:0.13em; color:#9990a8; text-transform:uppercase; padding:8px 0;">
        Add New Test Case
    </div>
    """, unsafe_allow_html=True)

    with st.form(key="add_benchmark"):
        new_space = st.selectbox("Genie Space", list(GENIE_SPACES.keys()), key="new_space")
        new_question = st.text_input("Question (natural language)", key="new_question", placeholder="e.g., How many titles are currently live?")
        new_table = st.text_input("Expected Table", key="new_table", placeholder="e.g., policy_window_snapshots")
        new_sql = st.text_area("Validation SQL (known-correct query)", key="new_sql", height=100, placeholder="SELECT COUNT(*) as answer FROM ...")
        col_ttype, col_tval = st.columns(2)
        with col_ttype:
            new_tol_type = st.selectbox("Tolerance Type", ["exact", "percentage", "contains"], key="new_tol_type")
        with col_tval:
            new_tol_val = st.number_input("Tolerance Value", value=0.0, min_value=0.0, step=0.01, key="new_tol_val")

        if st.form_submit_button("Add Benchmark", type="primary"):
            if not new_question or not new_sql:
                st.warning("Question and Validation SQL are required.")
            else:
                try:
                    _insert_benchmark(new_space, new_question, new_table, new_sql, new_tol_type, new_tol_val)
                    st.toast("Benchmark added!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to add benchmark: {e}")
