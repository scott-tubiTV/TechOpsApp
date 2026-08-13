"""Weekly Genie Health Check — combined benchmark + audit + report.

Runs every Friday (or on demand) to validate:
1. Genie accuracy (benchmark tests pass/fail)
2. Real conversation quality (audit recent sessions)
3. New error patterns to investigate

Usage:
    python benchmarks/weekly_check.py                    # full check
    python benchmarks/weekly_check.py --since 2026-07-28 # audit conversations since date
    python benchmarks/weekly_check.py --benchmark-only   # skip audit
    python benchmarks/weekly_check.py --audit-only       # skip benchmarks
    python benchmarks/weekly_check.py --dry-run          # no SQL execution for audit
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta

CONFIG_PATH = "benchmarks/config.json"
WAREHOUSE_ID = "a6b9541289d75c6e"
CONVERSATIONS_TABLE = "core_dev.techops.argo_conversations"

PRE_AGGREGATED_TABLES = [
    "imported_titles_monthly",
    "qa_abf_summary",
    "redelivery_weekly",
    "ops_weekly_workload",
]

SNAPSHOT_TABLES = [
    "avails_status_snapshot",
    "content_refresh_status",
    "partner_summary",
    "partner_title_status",
    "policy_window_snapshots",
    "ops_weekly_workload",
    "content_refresh_first_observed",
]

DATE_TRUNC_PATTERN = "date_trunc(month,"


def api_post(endpoint, body):
    result = subprocess.run(
        ["databricks", "api", "post", endpoint, "--json", json.dumps(body)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"API POST failed: {result.stderr}")
    return json.loads(result.stdout)


def api_get(endpoint):
    result = subprocess.run(
        ["databricks", "api", "get", endpoint],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"API GET failed: {result.stderr}")
    return json.loads(result.stdout)


def run_sql(sql):
    resp = api_post("/api/2.0/sql/statements", {
        "statement": sql,
        "warehouse_id": WAREHOUSE_ID,
        "wait_timeout": "50s",
    })
    status = resp.get("status", {}).get("state")
    if status in ("PENDING", "RUNNING"):
        stmt_id = resp.get("statement_id")
        for _ in range(30):
            time.sleep(2)
            resp = api_get(f"/api/2.0/sql/statements/{stmt_id}")
            status = resp.get("status", {}).get("state")
            if status not in ("PENDING", "RUNNING"):
                break
    if status != "SUCCEEDED":
        error = resp.get("status", {}).get("error", {}).get("message", "Unknown")
        return None, error
    columns = [c["name"] for c in resp.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = resp.get("result", {}).get("data_array", [])
    return [dict(zip(columns, row)) for row in rows], None


def ask_genie(space_id, question):
    try:
        resp = api_post(
            f"/api/2.0/genie/spaces/{space_id}/start-conversation",
            {"content": question},
        )
    except Exception as e:
        return None, str(e)

    conversation_id = resp.get("conversation_id")
    message_id = resp.get("message_id")
    if not conversation_id or not message_id:
        return None, "No conversation_id/message_id returned"

    for _ in range(30):
        time.sleep(2)
        try:
            result = api_get(
                f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}"
            )
        except Exception as e:
            return None, f"Poll failed: {e}"

        status = result.get("status")
        if status == "COMPLETED":
            for att in result.get("attachments", []):
                query_obj = att.get("query")
                if query_obj and query_obj.get("query"):
                    return query_obj["query"], None
            return None, "Genie completed but no SQL generated"
        elif status in ("FAILED", "CANCELLED"):
            return None, f"Genie status: {status}"

    return None, "Timeout waiting for Genie"


# ─── BENCHMARK SECTION ────────────────────────────────────────────────────────


def run_benchmarks():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    tests = config["tests"]
    results = {"passed": [], "failed": [], "errors": []}

    print(f"\n  Running {len(tests)} benchmark tests...")
    print(f"  {'─'*50}")

    for test in tests:
        space_name = test["space"]
        space_id = config["spaces"][space_name]["id"]
        test_id = test["id"]

        print(f"    {test_id}...", end="", flush=True)

        generated_sql, error = ask_genie(space_id, test["question"])
        if error:
            print(f" ERROR ({error[:40]})")
            results["errors"].append({"id": test_id, "detail": error, "question": test["question"]})
            continue

        genie_rows, err = run_sql(generated_sql)
        if err:
            print(f" SQL ERROR")
            results["errors"].append({"id": test_id, "detail": err, "sql": generated_sql, "question": test["question"]})
            continue

        validation_rows, err = run_sql(test["validation_sql"])
        if err:
            print(f" VALIDATION ERROR")
            results["errors"].append({"id": test_id, "detail": f"Validation SQL failed: {err}", "question": test["question"]})
            continue

        status, detail = _check_benchmark(test, generated_sql, genie_rows, validation_rows)

        if status == "PASS":
            print(f" PASS")
            results["passed"].append({"id": test_id, "detail": detail})
        else:
            print(f" FAIL ({detail[:50]})")
            results["failed"].append({"id": test_id, "detail": detail, "sql": generated_sql, "question": test["question"]})

    return results


def _check_benchmark(test, generated_sql, genie_rows, validation_rows):
    check_type = test.get("check", "percentage")
    expected_table = test.get("expected_table", "")

    if check_type == "table_used":
        if generated_sql and expected_table.lower() in generated_sql.lower():
            return "PASS", f"Uses {expected_table}"
        return "FAIL", f"Expected table '{expected_table}' not in SQL"

    genie_val = _extract_scalar(genie_rows)
    expected_val = _extract_scalar(validation_rows)

    if genie_val is None or expected_val is None:
        return "ERROR", f"Could not extract values (genie={genie_val}, expected={expected_val})"

    if check_type == "exact":
        if genie_val == expected_val:
            return "PASS", f"Exact: {int(genie_val)}"
        return "FAIL", f"Genie={int(genie_val)}, Expected={int(expected_val)}"

    tolerance = test.get("tolerance", 0.05)
    if expected_val == 0:
        return ("PASS", "Both zero") if genie_val == 0 else ("FAIL", f"Expected 0, got {int(genie_val)}")
    pct = abs(genie_val - expected_val) / abs(expected_val)
    if pct <= tolerance:
        return "PASS", f"{int(genie_val)} vs {int(expected_val)} ({pct*100:.1f}%)"
    return "FAIL", f"Genie={int(genie_val)}, Expected={int(expected_val)} (diff {pct*100:.1f}%)"


def _extract_scalar(rows):
    if not rows:
        return None
    first_val = list(rows[0].values())[0]
    try:
        return float(first_val) if first_val is not None else None
    except (ValueError, TypeError):
        return None


# ─── AUDIT SECTION ────────────────────────────────────────────────────────────


def run_audit(since_date=None, dry_run=False):
    where = "is_archived = false"
    if since_date:
        where += f" AND updated_at >= '{since_date}'"

    rows, err = run_sql(
        f"SELECT conversation_id, user_email, title, messages_json, updated_at "
        f"FROM {CONVERSATIONS_TABLE} WHERE {where} ORDER BY updated_at DESC"
    )
    if err:
        print(f"  ERROR loading conversations: {err}")
        return {"conversations": 0, "pairs": 0, "issues": []}

    conversations = rows or []
    print(f"  Found {len(conversations)} conversations since {since_date or 'beginning'}")
    print(f"  {'─'*50}")

    total_pairs = 0
    all_issues = []

    for conv in conversations:
        pairs = _extract_qa_pairs(conv.get("messages_json", ""))
        total_pairs += len(pairs)

        for pair in pairs:
            issues = _audit_pair(pair, dry_run)
            if issues:
                all_issues.append({
                    "conversation_id": conv["conversation_id"],
                    "user": conv.get("user_email", "?"),
                    "date": conv.get("updated_at", "?")[:10],
                    "question": pair["question"][:80],
                    "issues": issues,
                    "sql": pair["sql"][:200],
                })

    return {"conversations": len(conversations), "pairs": total_pairs, "issues": all_issues}


def _extract_qa_pairs(messages_json):
    try:
        messages = json.loads(messages_json)
    except (json.JSONDecodeError, TypeError):
        return []

    pairs = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("sql"):
            question = None
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "user" and messages[j].get("content"):
                    question = messages[j]["content"]
                    break
            if question:
                pairs.append({"question": question, "sql": msg["sql"]})
    return pairs


def _audit_pair(pair, dry_run):
    sql = pair["sql"]
    sql_lower = sql.lower()
    issues = []

    # Check 1: COUNT(*) on pre-aggregated tables
    for table in PRE_AGGREGATED_TABLES:
        if table.lower() in sql_lower:
            if "count(*)" in sql_lower and "sum(" not in sql_lower:
                issues.append(f"COUNT(*) on pre-aggregated '{table}' — should use SUM(count)")

    # Check 2: Missing snapshot_date filter
    for table in SNAPSHOT_TABLES:
        if table.lower() in sql_lower:
            if "snapshot_date" not in sql_lower:
                issues.append(f"No snapshot_date filter on '{table}'")

    # Check 3: Unquoted date_trunc — only flag if neither single nor double quotes present
    if "date_trunc(" in sql_lower:
        import re
        trunc_calls = re.findall(r"date_trunc\(\s*([^,)]+)", sql_lower)
        for arg in trunc_calls:
            arg_stripped = arg.strip()
            if not (arg_stripped.startswith("'") or arg_stripped.startswith('"')):
                issues.append("Unquoted date_trunc(month, ...) — causes UNRESOLVED_COLUMN errors")
                break

    # Check 4: snapshot_date >= instead of = MAX
    if "snapshot_date >=" in sql_lower and "max(snapshot_date)" not in sql_lower:
        issues.append("Uses snapshot_date >= instead of = (SELECT MAX(snapshot_date)...)")

    # Check 5: SQL validity (only if no static issues and not dry-run)
    if not dry_run and not issues:
        clean = sql.rstrip().rstrip(";")
        test_sql = f"SELECT * FROM ({clean}) _audit LIMIT 1"
        _, err = run_sql(test_sql)
        if err:
            if "PARSE_SYNTAX_ERROR" in (err or ""):
                test_sql2 = f"{clean} LIMIT 1"
                _, err2 = run_sql(test_sql2)
                if err2:
                    issues.append(f"SQL errors: {err2[:100]}")
            else:
                issues.append(f"SQL errors: {err[:100]}")

    # Check 6: Canonical query mismatch — detect when Genie SQL is missing critical filters
    question = pair.get("question", "").lower()
    canonical_issues = _check_canonical_patterns(question, sql_lower)
    issues.extend(canonical_issues)

    return issues


# Canonical patterns: question keywords → required SQL patterns
CANONICAL_PATTERNS = [
    {
        "name": "ABF backlog should include both statuses",
        "question_keywords": ["backlog", "pending review", "queue", "waiting for review"],
        "question_context": ["abf", "review"],  # at least one of these must also appear
        "required_sql": ["pending_review", "internal_review"],
        "require_all": True,
        "explanation": "ABF backlog = PENDING_REVIEW + INTERNAL_REVIEW. Query only filters one status.",
    },
    {
        "name": "ABF queue must use current_queue metric_type",
        "question_keywords": ["queue", "backlog", "pending"],
        "question_context": ["abf"],
        "required_sql": ["current_queue"],
        "require_all": True,
        "explanation": "ABF queue questions should filter metric_type = 'current_queue', not monthly_review.",
    },
    {
        "name": "Live titles must use policy_window_snapshots",
        "question_keywords": ["live", "streaming"],
        "question_context": ["titles", "how many"],
        "required_sql": ["policy_window_snapshots"],
        "require_all": True,
        "explanation": "Live title counts require policy_window_snapshots, not avails_status_snapshot.",
    },
    {
        "name": "New avails must filter by inserted_at",
        "question_keywords": ["new avails", "avails created"],
        "question_context": ["month", "week", "today"],
        "required_sql": ["inserted_at"],
        "require_all": True,
        "explanation": "New avails = filtered by inserted_at date range, not just status.",
    },
    {
        "name": "Imports should use imported_titles_monthly not avails",
        "question_keywords": ["imported", "imports"],
        "question_context": ["titles", "month"],
        "required_sql": ["imported_titles_monthly"],
        "require_all": True,
        "explanation": "Import volume should use imported_titles_monthly, not avails_status_snapshot.",
    },
    {
        "name": "Redeliveries must filter dismissed and active",
        "question_keywords": ["open redeliveries", "active redeliveries"],
        "question_context": [],
        "required_sql_present": ["redeliveries"],
        "required_sql": ["dismissed", "is_active_redelivery"],
        "require_all": True,
        "explanation": "Open redeliveries must filter dismissed = false AND is_active_redelivery = true.",
    },
    {
        "name": "Pre-aggregated tables need SUM not COUNT",
        "question_keywords": ["how many", "total", "count"],
        "question_context": ["abf", "review"],
        "required_sql_absent": ["count(*)"],  # should NOT appear if qa_abf_summary is used
        "required_sql_present": ["qa_abf_summary"],
        "explanation": "qa_abf_summary is pre-aggregated — use SUM(count) not COUNT(*).",
    },
]


def _check_canonical_patterns(question, sql_lower):
    issues = []
    for pattern in CANONICAL_PATTERNS:
        # Check if question matches this pattern
        has_keyword = any(kw in question for kw in pattern["question_keywords"])
        if not has_keyword:
            continue

        has_context = (
            not pattern["question_context"]
            or any(ctx in question for ctx in pattern["question_context"])
        )
        if not has_context:
            continue

        # Check required SQL patterns (only if prerequisite table is present)
        if "required_sql" in pattern:
            prereq = pattern.get("required_sql_present")
            if prereq and not all(p in sql_lower for p in prereq):
                continue  # table not used in this query, skip check

            if pattern.get("require_all"):
                missing = [p for p in pattern["required_sql"] if p not in sql_lower]
                if missing:
                    issues.append(
                        f"Canonical mismatch ({pattern['name']}) — {pattern['explanation']} "
                        f"Missing: {', '.join(missing)}"
                    )
            else:
                if not any(p in sql_lower for p in pattern["required_sql"]):
                    issues.append(
                        f"Canonical mismatch ({pattern['name']}) — {pattern['explanation']}"
                    )

        # Check patterns that should be absent
        if "required_sql_absent" in pattern and "required_sql_present" in pattern:
            if all(p in sql_lower for p in pattern["required_sql_present"]):
                for bad in pattern["required_sql_absent"]:
                    if bad in sql_lower:
                        issues.append(
                            f"Canonical mismatch ({pattern['name']}) — {pattern['explanation']}"
                        )
                        break

    return issues


# ─── REPORT ───────────────────────────────────────────────────────────────────


def print_report(benchmark_results, audit_results, duration_secs):
    print(f"\n{'═'*60}")
    print(f"  WEEKLY GENIE HEALTH CHECK — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'═'*60}")

    # Benchmark summary
    if benchmark_results:
        total = len(benchmark_results["passed"]) + len(benchmark_results["failed"]) + len(benchmark_results["errors"])
        pass_rate = len(benchmark_results["passed"]) / total * 100 if total else 0
        print(f"\n  BENCHMARKS: {len(benchmark_results['passed'])}/{total} passed ({pass_rate:.0f}%)")

        if benchmark_results["failed"]:
            print(f"\n  Failed tests:")
            for f in benchmark_results["failed"]:
                print(f"    ✗ {f['id']}: {f['detail'][:60]}")

        if benchmark_results["errors"]:
            print(f"\n  Errors:")
            for e in benchmark_results["errors"]:
                print(f"    ⚠ {e['id']}: {e['detail'][:60]}")

    # Audit summary
    if audit_results:
        print(f"\n  AUDIT: {audit_results['conversations']} conversations, "
              f"{audit_results['pairs']} Q&A pairs checked")
        issue_count = len(audit_results["issues"])

        if issue_count == 0:
            print(f"  ✓ No issues detected")
        else:
            print(f"  ⚠ {issue_count} issues found:")

            # Group by issue type
            issue_types = {}
            for item in audit_results["issues"]:
                for issue in item["issues"]:
                    key = issue.split("—")[0].strip() if "—" in issue else issue[:50]
                    if key not in issue_types:
                        issue_types[key] = []
                    issue_types[key].append(item)

            for issue_type, items in sorted(issue_types.items(), key=lambda x: -len(x[1])):
                print(f"\n    [{len(items)}x] {issue_type}")
                for item in items[:3]:
                    print(f"        {item['user']} ({item['date']}): \"{item['question']}\"")
                if len(items) > 3:
                    print(f"        ... and {len(items) - 3} more")

    # Deep-dive recommendations
    deep_dives = _identify_deep_dives(benchmark_results, audit_results)
    if deep_dives:
        print(f"\n  {'─'*50}")
        print(f"  DEEP-DIVE RECOMMENDATIONS:")
        for dd in deep_dives:
            print(f"    → {dd}")

    print(f"\n  Duration: {duration_secs:.0f}s")
    print(f"{'═'*60}\n")


def _identify_deep_dives(benchmark_results, audit_results):
    deep_dives = []

    if benchmark_results:
        # New failures (things that used to pass)
        if benchmark_results["failed"]:
            deep_dives.append(
                f"{len(benchmark_results['failed'])} benchmark failures — "
                f"check if Genie instructions need update"
            )

    if audit_results and audit_results["issues"]:
        issue_types = {}
        for item in audit_results["issues"]:
            for issue in item["issues"]:
                key = issue.split("—")[0].strip() if "—" in issue else issue.split(":")[0]
                issue_types[key] = issue_types.get(key, 0) + 1

        # Recurring patterns
        for issue_type, count in issue_types.items():
            if count >= 3:
                deep_dives.append(f"Recurring: \"{issue_type}\" ({count} occurrences) — may need instruction fix")

        # Users hitting errors
        affected_users = set(item["user"] for item in audit_results["issues"])
        if affected_users:
            deep_dives.append(f"Affected users: {', '.join(sorted(affected_users))}")

    if not deep_dives:
        deep_dives.append("No systemic issues — Genie is healthy")

    return deep_dives


# ─── ACCURACY METRICS ────────────────────────────────────────────────────────

ACCURACY_TABLE = "core_dev.techops.genie_accuracy"


def compute_accuracy(audit_results):
    """Compute accuracy metrics from audit results."""
    if not audit_results or audit_results["pairs"] == 0:
        return None

    total_pairs = audit_results["pairs"]
    # Count unique Q&A pairs with at least one issue
    pairs_with_issues = len(audit_results["issues"])
    clean_pairs = total_pairs - pairs_with_issues
    accuracy_pct = (clean_pairs / total_pairs) * 100 if total_pairs > 0 else 0

    # Break down by issue type
    issue_breakdown = {}
    for item in audit_results["issues"]:
        for issue in item["issues"]:
            key = issue.split("—")[0].strip() if "—" in issue else issue.split(":")[0].strip()
            issue_breakdown[key] = issue_breakdown.get(key, 0) + 1

    return {
        "total_pairs": total_pairs,
        "clean_pairs": clean_pairs,
        "pairs_with_issues": pairs_with_issues,
        "accuracy_pct": round(accuracy_pct, 1),
        "conversations": audit_results["conversations"],
        "issue_breakdown": issue_breakdown,
    }


def persist_accuracy(accuracy, since_date):
    """Write daily accuracy snapshot to Delta table."""
    if not accuracy:
        return

    # Ensure table exists
    create_sql = f"""
        CREATE TABLE IF NOT EXISTS {ACCURACY_TABLE} (
            snapshot_date DATE,
            audit_window_start STRING,
            total_conversations INT,
            total_pairs INT,
            clean_pairs INT,
            pairs_with_issues INT,
            accuracy_pct DOUBLE,
            issue_breakdown STRING,
            created_at TIMESTAMP
        ) USING DELTA
    """
    run_sql(create_sql)

    breakdown_json = json.dumps(accuracy["issue_breakdown"]).replace("'", "''")

    insert_sql = f"""
        MERGE INTO {ACCURACY_TABLE} AS target
        USING (SELECT current_date() AS snapshot_date) AS source
        ON target.snapshot_date = source.snapshot_date
        WHEN MATCHED THEN UPDATE SET
            audit_window_start = '{since_date}',
            total_conversations = {accuracy['conversations']},
            total_pairs = {accuracy['total_pairs']},
            clean_pairs = {accuracy['clean_pairs']},
            pairs_with_issues = {accuracy['pairs_with_issues']},
            accuracy_pct = {accuracy['accuracy_pct']},
            issue_breakdown = '{breakdown_json}',
            created_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT
            (snapshot_date, audit_window_start, total_conversations, total_pairs,
             clean_pairs, pairs_with_issues, accuracy_pct, issue_breakdown, created_at)
        VALUES
            (current_date(), '{since_date}', {accuracy['conversations']},
             {accuracy['total_pairs']}, {accuracy['clean_pairs']},
             {accuracy['pairs_with_issues']}, {accuracy['accuracy_pct']},
             '{breakdown_json}', current_timestamp())
    """
    _, err = run_sql(insert_sql)
    if err:
        print(f"  WARNING: Could not persist accuracy: {err}")
    else:
        print(f"  ✓ Accuracy saved to {ACCURACY_TABLE}: {accuracy['accuracy_pct']}%")


# ─── MAIN ─────────────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]
    since_date = None
    benchmark_only = False
    audit_only = False
    dry_run = False

    i = 0
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            since_date = args[i + 1]
            i += 2
        elif args[i] == "--benchmark-only":
            benchmark_only = True
            i += 1
        elif args[i] == "--audit-only":
            audit_only = True
            i += 1
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    if not since_date:
        since_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    start = time.time()

    print(f"\n{'═'*60}")
    print(f"  WEEKLY GENIE HEALTH CHECK")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Audit window: since {since_date}")
    if dry_run:
        print(f"  Mode: DRY RUN")
    print(f"{'═'*60}")

    benchmark_results = None
    audit_results = None

    if not audit_only:
        print(f"\n  ┌─ PHASE 1: Benchmarks")
        benchmark_results = run_benchmarks()

    if not benchmark_only:
        print(f"\n  ┌─ PHASE 2: Conversation Audit")
        audit_results = run_audit(since_date=since_date, dry_run=dry_run)

        # Compute and persist accuracy
        accuracy = compute_accuracy(audit_results)
        if accuracy and not dry_run:
            print(f"\n  ┌─ PHASE 3: Accuracy Metrics")
            print(f"  Accuracy: {accuracy['accuracy_pct']}% "
                  f"({accuracy['clean_pairs']}/{accuracy['total_pairs']} pairs clean)")
            persist_accuracy(accuracy, since_date)

    duration = time.time() - start
    print_report(benchmark_results, audit_results, duration)

    has_issues = False
    if benchmark_results and (benchmark_results["failed"] or benchmark_results["errors"]):
        has_issues = True
    if audit_results and audit_results["issues"]:
        has_issues = True

    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
