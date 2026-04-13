"""Tubi Title Detective - Duplicate & Conflict Detection App."""

import os
import io
import csv
import json
import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from databricks.sdk import WorkspaceClient
from thefuzz import fuzz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Tubi Title Detective")

# --- Config ---
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "f6585a1131b2b962")
UC_CATALOG = os.environ.get("UC_CATALOG", "tubi_content_ops_catalog")
UC_SCHEMA = os.environ.get("UC_SCHEMA", "demo_schema")
LAKEBASE_PROJECT = os.environ.get("LAKEBASE_PROJECT", "tubi-content-ops")
LAKEBASE_DB = os.environ.get("LAKEBASE_DB", "tubi_content_ops")

# Thresholds
DUPLICATE_THRESHOLD = 85  # fuzzy match score for duplicate detection
CONFLICT_OVERLAP_DAYS = 0  # any overlap in same country = conflict

w = WorkspaceClient()


LAKEBASE_HOST = os.environ.get(
    "LAKEBASE_HOST",
    "ep-polished-dream-d20p689h.database.us-east-1.cloud.databricks.com",
)


def get_lakebase_conn():
    """Get a connection to Lakebase Postgres."""
    endpoint_path = f"projects/{LAKEBASE_PROJECT}/branches/production/endpoints/primary"

    # Use the Databricks SDK to generate a database credential
    cred = w.api_client.do(
        "POST",
        "/api/2.0/postgres/credentials",
        body={"endpoint": endpoint_path},
    )
    token = cred.get("token", "")
    email = w.current_user.me().user_name

    return psycopg2.connect(
        host=LAKEBASE_HOST, port=5432, database=LAKEBASE_DB,
        user=email, password=token, sslmode="require",
    )


def query_uc(sql: str) -> list[dict]:
    """Execute a query against Unity Catalog via SQL warehouse."""
    try:
        response = w.statement_execution.execute_statement(
            warehouse_id=WAREHOUSE_ID,
            statement=sql,
            wait_timeout="30s",
        )
        if response.status and response.status.state.value == "SUCCEEDED":
            columns = [col.name for col in response.manifest.schema.columns]
            rows = []
            if response.result and response.result.data_array:
                for row in response.result.data_array:
                    rows.append(dict(zip(columns, row)))
            return rows
        else:
            error = response.status.error if response.status else "Unknown error"
            logger.error(f"SQL error: {error}")
            return []
    except Exception as e:
        logger.error(f"UC query error: {e}")
        return []


def detect_duplicates_and_conflicts(uploaded_titles: list[dict], master_titles: list[dict]) -> list[dict]:
    """Compare uploaded titles against master catalog for duplicates and conflicts."""
    results = []

    for uploaded in uploaded_titles:
        best_match = None
        best_score = 0
        detection_type = "clean"
        conflict_reason = None

        for master in master_titles:
            # Fuzzy match on title name
            name_score = fuzz.ratio(
                (uploaded.get("title_name") or "").lower(),
                (master.get("title_name") or "").lower()
            )

            # Boost score if director matches
            director_score = fuzz.ratio(
                (uploaded.get("director") or "").lower(),
                (master.get("director") or "").lower()
            )

            # Boost if release year matches
            year_match = 1.0 if str(uploaded.get("release_year", "")) == str(master.get("release_year", "")) else 0.0

            # Weighted composite score
            composite = (name_score * 0.5) + (director_score * 0.3) + (year_match * 20)

            if composite > best_score:
                best_score = composite
                best_match = master

        if best_match and best_score >= DUPLICATE_THRESHOLD:
            # Check if it's a conflict (same country, overlapping license dates)
            same_country = (uploaded.get("country") or "").upper() == (best_match.get("country") or "").upper()
            overlap = check_date_overlap(
                uploaded.get("license_start_date"), uploaded.get("license_end_date"),
                best_match.get("license_start_date"), best_match.get("license_end_date"),
            )

            if same_country and overlap:
                detection_type = "conflict"
                conflict_reason = (
                    f"License overlap in {uploaded.get('country')}: "
                    f"uploaded [{uploaded.get('license_start_date')} to {uploaded.get('license_end_date')}] "
                    f"vs existing [{best_match.get('license_start_date')} to {best_match.get('license_end_date')}] "
                    f"from {best_match.get('import_source')}"
                )
            else:
                detection_type = "duplicate"
                if not same_country:
                    # Different country = might be a legitimate new territory license
                    detection_type = "duplicate"
                    conflict_reason = (
                        f"Same title exists from {best_match.get('import_source')} "
                        f"in {best_match.get('country')}. "
                        f"New upload is for {uploaded.get('country')} from {uploaded.get('import_source')}."
                    )
                else:
                    conflict_reason = (
                        f"Exact/near match found from {best_match.get('import_source')} "
                        f"(score: {best_score:.0f}). No date overlap."
                    )

        uploaded["detection_type"] = detection_type
        uploaded["confidence_score"] = round(best_score, 2) if best_match else 0
        uploaded["matched_title_id"] = best_match.get("title_id") if best_match else None
        uploaded["conflict_reason"] = conflict_reason
        uploaded["matched_title"] = best_match
        results.append(uploaded)

    return results


def check_date_overlap(start1, end1, start2, end2) -> bool:
    """Check if two date ranges overlap."""
    try:
        from datetime import date

        def parse(d):
            if isinstance(d, date):
                return d
            if d is None:
                return None
            return datetime.strptime(str(d), "%Y-%m-%d").date()

        s1, e1, s2, e2 = parse(start1), parse(end1), parse(start2), parse(end2)
        if not all([s1, e1, s2, e2]):
            return False
        return s1 <= e2 and s2 <= e1
    except Exception:
        return False


# --- API Routes ---

@app.get("/api/health")
def health():
    return {"status": "ok", "app": "Tubi Title Detective"}


@app.get("/api/master-titles")
def get_master_titles():
    """Get all titles from the UC master catalog."""
    sql = f"SELECT * FROM {UC_CATALOG}.{UC_SCHEMA}.titles_master ORDER BY title_name"
    titles = query_uc(sql)
    return {"titles": titles, "count": len(titles)}


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)):
    """Upload a CSV file and detect duplicates/conflicts."""
    if not file.filename.endswith((".csv", ".CSV")):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    content = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {str(e)}")

    uploaded_titles = df.to_dict(orient="records")

    # Get master titles from UC
    sql = f"SELECT * FROM {UC_CATALOG}.{UC_SCHEMA}.titles_master"
    master_titles = query_uc(sql)

    # Run detection
    results = detect_duplicates_and_conflicts(uploaded_titles, master_titles)

    # Count results
    duplicates = [r for r in results if r["detection_type"] == "duplicate"]
    conflicts = [r for r in results if r["detection_type"] == "conflict"]
    clean = [r for r in results if r["detection_type"] == "clean"]

    # Save session to Lakebase (best-effort — detection works without it)
    session_id = None
    try:
        conn = get_lakebase_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO upload_sessions
                   (session_name, file_name, uploaded_by, total_titles, duplicates_found,
                    conflicts_found, clean_titles, status, created_at, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                   RETURNING id""",
                (
                    f"Upload - {file.filename}",
                    file.filename,
                    "demo_user",
                    len(results),
                    len(duplicates),
                    len(conflicts),
                    len(clean),
                    "completed",
                ),
            )
            session_id = cur.fetchone()[0]

            for r in results:
                cur.execute(
                    """INSERT INTO uploaded_titles
                       (session_id, title_name, original_title, content_type, release_year,
                        director, cast_members, genre, country, language, import_source,
                        license_start_date, license_end_date, detection_status, detection_type,
                        matched_title_id, confidence_score, conflict_reason, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                    (
                        session_id,
                        r.get("title_name"),
                        r.get("original_title"),
                        r.get("content_type"),
                        r.get("release_year"),
                        r.get("director"),
                        r.get("cast_members"),
                        r.get("genre"),
                        r.get("country"),
                        r.get("language"),
                        r.get("import_source"),
                        r.get("license_start_date"),
                        r.get("license_end_date"),
                        "detected",
                        r.get("detection_type"),
                        r.get("matched_title_id"),
                        r.get("confidence_score"),
                        r.get("conflict_reason"),
                    ),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Lakebase save error: {e}")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Lakebase connection unavailable: {e}. Detection still works.")

    # Strip matched_title from response (too verbose for summary)
    for r in results:
        if r.get("matched_title"):
            r["matched_title_name"] = r["matched_title"].get("title_name")
            r["matched_import_source"] = r["matched_title"].get("import_source")
            r["matched_country"] = r["matched_title"].get("country")
            del r["matched_title"]

    return {
        "session_id": session_id,
        "file_name": file.filename,
        "total": len(results),
        "duplicates": len(duplicates),
        "conflicts": len(conflicts),
        "clean": len(clean),
        "results": results,
    }


@app.post("/api/resolve")
async def resolve_title(body: dict):
    """Resolve a duplicate or conflict."""
    action_type = body.get("action_type")  # keep_existing, replace, keep_both, skip
    title_data = body.get("title_data", {})

    if not action_type:
        raise HTTPException(status_code=400, detail="Missing action_type")

    # If action is to add the title to the master catalog, write to UC directly
    if action_type in ("replace", "keep_both") and title_data:
        insert_sql = f"""
            INSERT INTO {UC_CATALOG}.{UC_SCHEMA}.titles_master
            (title_name, original_title, content_type, release_year, director,
             cast_members, genre, country, language, import_source,
             license_start_date, license_end_date, status, created_at, updated_at)
            VALUES ('{_esc(title_data.get("title_name"))}', '{_esc(title_data.get("original_title"))}',
                    '{_esc(title_data.get("content_type"))}', {title_data.get("release_year") or "NULL"},
                    '{_esc(title_data.get("director"))}', '{_esc(title_data.get("cast_members"))}',
                    '{_esc(title_data.get("genre"))}', '{_esc(title_data.get("country"))}',
                    '{_esc(title_data.get("language"))}', '{_esc(title_data.get("import_source"))}',
                    '{title_data.get("license_start_date")}', '{title_data.get("license_end_date")}',
                    'active', current_timestamp(), current_timestamp())
        """
        query_uc(insert_sql)

    # Best-effort Lakebase logging
    try:
        conn = get_lakebase_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO resolution_actions
                   (session_id, uploaded_title_id, action_type, action_detail, resolved_by, created_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())""",
                (body.get("session_id", 0), body.get("uploaded_title_id", 0),
                 action_type, body.get("action_detail", ""), "demo_user"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()
    except Exception:
        pass

    return {"status": "resolved", "action": action_type}


def _esc(val):
    """Escape single quotes for SQL."""
    if val is None:
        return ""
    return str(val).replace("'", "''")


@app.get("/api/sessions")
def get_sessions():
    """Get upload session history from Lakebase."""
    conn = get_lakebase_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM upload_sessions ORDER BY created_at DESC LIMIT 20")
        sessions = cur.fetchall()
        # Convert to serializable
        return {"sessions": [dict(s) for s in sessions]}
    except Exception as e:
        logger.error(f"Sessions query error: {e}")
        return {"sessions": []}
    finally:
        conn.close()


@app.get("/api/sessions/{session_id}/results")
def get_session_results(session_id: int):
    """Get detection results for a session."""
    conn = get_lakebase_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM uploaded_titles WHERE session_id = %s ORDER BY id",
            (session_id,),
        )
        titles = cur.fetchall()

        cur.execute(
            "SELECT * FROM resolution_actions WHERE session_id = %s ORDER BY created_at",
            (session_id,),
        )
        actions = cur.fetchall()

        return {
            "titles": [dict(t) for t in titles],
            "actions": [dict(a) for a in actions],
        }
    except Exception as e:
        logger.error(f"Results query error: {e}")
        return {"titles": [], "actions": []}
    finally:
        conn.close()


@app.get("/api/sample-csv")
def get_sample_csv():
    """Return the sample CSV for demo purposes."""
    csv_content = """title_name,original_title,content_type,release_year,director,cast_members,genre,country,language,import_source,license_start_date,license_end_date
The Last Heist,The Last Heist,movie,2022,Sarah Chen,"John Davis, Maria Lopez, James Wright",Action,US,en,FilmRise,2024-06-01,2025-12-31
Midnight in Paris,Minuit à Paris,movie,2011,Woody Allen,"Owen Wilson, Rachel McAdams",Romance,US,en,FilmRise,2024-05-01,2025-08-31
Neon Nights,Neon Nights,movie,2023,Akira Tanaka,"Emma Stone, Ken Watanabe",Sci-Fi,US,en,Gravitas Ventures,2025-01-01,2026-06-30
Dark Waters,Dark Waters,movie,2020,Elena Rodriguez,"Chris Evans, Lupita Nyongo",Horror,MX,es,Cinelatino,2024-06-01,2025-09-30
Street Kings,Street Kings,movie,2019,Marcus Johnson,"Denzel Howard, Lisa Park",Drama,US,en,Magnolia Pictures,2024-03-01,2026-01-31
Blood Moon Rising,Blood Moon Rising,movie,2023,James Wan,"Florence Pugh, Oscar Isaac",Horror,US,en,Screen Media,2025-01-01,2026-01-01
Sunset Boulevard Redux,Sunset Boulevard Redux,movie,2024,Sofia Coppola,"Margot Robbie, Ryan Gosling",Drama,US,en,FilmRise,2025-03-01,2026-03-01
Pacific Rim: New Dawn,Pacific Rim: New Dawn,movie,2025,Guillermo del Toro,"John Boyega, Cailee Spaeny",Sci-Fi,US,en,Legendary Entertainment,2025-06-01,2026-12-31
The Last Heist,El Último Golpe,movie,2022,Sarah Chen,"John Davis, Maria Lopez, James Wright",Action,MX,es,FilmRise LATAM,2024-08-01,2025-11-30
Seoul Train,서울행,movie,2021,Park Jin-woo,"Song Kang-ho, Jeon Do-yeon",Action,US,en,CJ Entertainment USA,2024-06-01,2025-10-31"""
    return JSONResponse(
        content={"csv": csv_content},
        headers={"Content-Type": "application/json"},
    )


# Serve static HTML frontend
@app.get("/")
async def serve_frontend():
    html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "index.html")
    with open(html_path, "r") as f:
        return HTMLResponse(content=f.read())
