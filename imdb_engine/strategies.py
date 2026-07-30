"""Matching strategies — each encapsulates one approach to finding IMDB IDs."""

from .config import IMDBEngineConfig
from .models import Confidence, MatchMethod, MatchRequest, MatchResult
from .normalizer import escape_sql, make_normalized_key, normalize, normalize_no_article
from .sql_client import SQLClient


class VerifiedLookupStrategy:
    """Check the verification Delta table. Always wins."""

    def __init__(self, config: IMDBEngineConfig):
        self.config = config

    def match(self, requests: list, sql_client: SQLClient, verified_lookup: dict) -> dict:
        """Returns {request_index: MatchResult} for titles found in verification table."""
        results = {}
        for i, req in enumerate(requests):
            key = make_normalized_key(req.title, req.content_type, req.release_year)
            if key in verified_lookup:
                row = verified_lookup[key]
                results[i] = MatchResult(
                    request=req,
                    imdb_id=row["imdb_id"],
                    imdb_title=row.get("imdb_title"),
                    content_id=row.get("content_id"),
                    confidence=Confidence.VERIFIED,
                    match_method=MatchMethod.VERIFIED_LOOKUP,
                )
        return results


class DirectLookupStrategy:
    """Look up IMDB IDs from content_info and rich_content using content_ids."""

    def __init__(self, config: IMDBEngineConfig):
        self.config = config

    def match(self, requests: list, sql_client: SQLClient) -> dict:
        """Match titles to content_info, then get IMDB IDs from rich_content."""
        if not requests:
            return {}

        # Build VALUES clause for batch matching against content_info
        values = []
        for i, req in enumerate(requests):
            norm = normalize(req.title)
            norm_no_art = normalize_no_article(req.title)
            values.append(
                f"({i}, '{escape_sql(norm)}', '{escape_sql(norm_no_art)}', "
                f"'{escape_sql(req.content_type)}', '{escape_sql(req.release_year)}')"
            )

        chunks = [values[j : j + self.config.batch_size] for j in range(0, len(values), self.config.batch_size)]
        ci_matches = {}

        for chunk in chunks:
            values_sql = ",\n".join(chunk)
            rows = sql_client.execute(f"""
                WITH input_titles AS (
                    SELECT col1 AS idx, col2 AS norm_title, col3 AS norm_no_article,
                           col4 AS content_type, col5 AS release_year
                    FROM (VALUES {values_sql}) AS t(col1, col2, col3, col4, col5)
                ),
                ci AS (
                    SELECT
                        content_id, title, content_type, release_year, imdb_id, program_imdb_id,
                        LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(title, '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_title,
                        LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(
                            REGEXP_REPLACE(title, '(?i)^(the|a|an)\\\\s+', ''),
                            '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_no_article
                    FROM {self.config.content_info_table}
                    WHERE active = true AND content_type IN ('MOVIE', 'SERIES')
                )
                SELECT
                    CAST(it.idx AS INT) AS idx,
                    ci.content_id,
                    ci.title AS ci_title,
                    ci.content_type AS ci_type,
                    ci.release_year AS ci_year,
                    ci.imdb_id AS ci_imdb_id,
                    ci.program_imdb_id,
                    CASE WHEN it.norm_title = ci.norm_title THEN 'exact'
                         WHEN it.norm_no_article = ci.norm_no_article THEN 'no_article'
                         ELSE 'other' END AS title_match,
                    CASE WHEN it.release_year != '' AND ci.release_year IS NOT NULL
                              AND CAST(it.release_year AS INT) = ci.release_year THEN 1 ELSE 0 END AS year_exact
                FROM input_titles it
                JOIN ci ON (it.norm_title = ci.norm_title OR it.norm_no_article = ci.norm_no_article)
                WHERE (it.content_type = '' OR it.content_type = ci.content_type)
                  AND (it.release_year = '' OR ci.release_year IS NULL
                       OR ABS(CAST(it.release_year AS INT) - ci.release_year) <= {self.config.year_tolerance})
                ORDER BY idx, year_exact DESC, title_match
            """)

            # Keep best match per index
            for row in rows:
                idx = int(row["idx"])
                if idx not in ci_matches:
                    ci_matches[idx] = row

        # For matched content_ids, check if imdb_id already exists in content_info
        results = {}
        cids_needing_rc = []

        for idx, row in ci_matches.items():
            req = requests[idx]
            imdb_from_ci = row.get("ci_imdb_id") or row.get("program_imdb_id")
            if imdb_from_ci and imdb_from_ci.strip():
                results[idx] = MatchResult(
                    request=req,
                    imdb_id=imdb_from_ci.strip(),
                    content_id=row["content_id"],
                    confidence=Confidence.HIGH,
                    match_method=MatchMethod.DIRECT_CONTENT_INFO,
                    metadata={"ci_title": row.get("ci_title"), "ci_year": row.get("ci_year")},
                )
            else:
                cids_needing_rc.append((idx, row["content_id"], row))

        # Batch lookup in rich_content
        if cids_needing_rc:
            cid_list = ", ".join(f"'{escape_sql(cid)}'" for _, cid, _ in cids_needing_rc)
            rc_rows = sql_client.execute(f"""
                SELECT tubi_video_id AS content_id, imdb_id
                FROM {self.config.rich_content_table}
                WHERE tubi_video_id IN ({cid_list})
                  AND imdb_id IS NOT NULL AND TRIM(imdb_id) != ''
            """)
            rc_map = {r["content_id"]: r["imdb_id"] for r in rc_rows}

            for idx, cid, ci_row in cids_needing_rc:
                if cid in rc_map:
                    req = requests[idx]
                    results[idx] = MatchResult(
                        request=req,
                        imdb_id=rc_map[cid],
                        content_id=cid,
                        confidence=Confidence.HIGH,
                        match_method=MatchMethod.DIRECT_RICH_CONTENT,
                        metadata={"ci_title": ci_row.get("ci_title"), "ci_year": ci_row.get("ci_year")},
                    )
                elif idx not in results:
                    # Resolved content_id but no IMDB — store content_id for cross-validation
                    req = requests[idx]
                    results[idx] = MatchResult(
                        request=req,
                        imdb_id=None,
                        content_id=cid,
                        confidence=Confidence.LOW,
                        match_method=MatchMethod.DIRECT_CONTENT_INFO,
                        metadata={"ci_title": ci_row.get("ci_title"), "ci_year": ci_row.get("ci_year"), "no_imdb": True},
                    )

        return results


class CatalogMatchStrategy:
    """Match against IMDB catalog. Handles both exact and fuzzy."""

    def __init__(self, config: IMDBEngineConfig):
        self.config = config

    def match(self, requests: list, sql_client: SQLClient) -> dict:
        if not requests:
            return {}

        values = []
        for i, req in enumerate(requests):
            norm = normalize(req.title)
            norm_no_art = normalize_no_article(req.title)
            values.append(
                f"({i}, '{escape_sql(norm)}', '{escape_sql(norm_no_art)}', "
                f"'{escape_sql(req.content_type)}', '{escape_sql(req.release_year)}')"
            )

        chunks = [values[j : j + self.config.batch_size] for j in range(0, len(values), self.config.batch_size)]
        all_matches = []

        for chunk in chunks:
            values_sql = ",\n".join(chunk)
            rows = sql_client.execute(f"""
                WITH input_titles AS (
                    SELECT col1 AS idx, col2 AS norm_title, col3 AS norm_no_article,
                           col4 AS content_type, col5 AS release_year
                    FROM (VALUES {values_sql}) AS t(col1, col2, col3, col4, col5)
                ),
                imdb AS (
                    SELECT
                        titleId AS imdb_id, originalTitle, year AS imdb_year, titleType,
                        CASE WHEN titleType IN ('movie', 'tvMovie', 'video') THEN 'MOVIE'
                             WHEN titleType IN ('tvSeries', 'tvMiniSeries') THEN 'SERIES'
                        END AS mapped_type,
                        LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(originalTitle, '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_title,
                        LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(
                            REGEXP_REPLACE(originalTitle, '(?i)^(the|a|an)\\\\s+', ''),
                            '[^a-zA-Z0-9\\\\s]', ''), '\\\\s+', ' '))) AS norm_no_article
                    FROM {self.config.imdb_catalog_table}
                    WHERE titleType IN ('movie', 'tvMovie', 'video', 'tvSeries', 'tvMiniSeries')
                ),
                matched AS (
                    SELECT
                        CAST(it.idx AS INT) AS idx,
                        imdb.imdb_id,
                        imdb.originalTitle AS imdb_title,
                        imdb.imdb_year,
                        imdb.titleType AS imdb_type,
                        CASE WHEN it.norm_title = imdb.norm_title THEN 'exact'
                             WHEN it.norm_no_article = imdb.norm_no_article THEN 'no_article'
                             ELSE 'other' END AS title_match,
                        CASE WHEN it.release_year != '' AND imdb.imdb_year IS NOT NULL
                                  AND CAST(it.release_year AS INT) = imdb.imdb_year THEN 'exact_year'
                             WHEN it.release_year != '' AND imdb.imdb_year IS NOT NULL
                                  AND ABS(CAST(it.release_year AS INT) - imdb.imdb_year) <= {self.config.year_tolerance} THEN 'year_close'
                             WHEN it.release_year = '' OR imdb.imdb_year IS NULL THEN 'year_missing'
                             ELSE 'year_mismatch' END AS year_match
                    FROM input_titles it
                    JOIN imdb ON (it.norm_title = imdb.norm_title OR it.norm_no_article = imdb.norm_no_article)
                    WHERE (it.content_type = '' OR it.content_type = imdb.mapped_type)
                      AND NOT (it.release_year != '' AND imdb.imdb_year IS NOT NULL
                               AND ABS(CAST(it.release_year AS INT) - imdb.imdb_year) > {self.config.year_tolerance})
                ),
                counted AS (
                    SELECT idx, COUNT(DISTINCT imdb_id) AS num_candidates FROM matched GROUP BY idx
                ),
                ranked AS (
                    SELECT m.*, c.num_candidates,
                        ROW_NUMBER() OVER (
                            PARTITION BY m.idx
                            ORDER BY
                                CASE WHEN m.year_match = 'exact_year' THEN 0
                                     WHEN m.year_match = 'year_close' THEN 1
                                     WHEN m.year_match = 'year_missing' THEN 2
                                     ELSE 3 END,
                                CASE WHEN m.title_match = 'exact' THEN 0 ELSE 1 END
                        ) AS rn
                    FROM matched m
                    JOIN counted c ON m.idx = c.idx
                )
                SELECT idx, imdb_id, imdb_title, imdb_year, imdb_type,
                       title_match, year_match, num_candidates
                FROM ranked WHERE rn = 1
            """)
            all_matches.extend(rows)

        results = {}
        for row in all_matches:
            idx = int(row["idx"])
            req = requests[idx]
            num_cand = int(row["num_candidates"])
            title_match = row["title_match"]
            year_match = row["year_match"]

            confidence = self._score_confidence(num_cand, title_match, year_match)
            method = MatchMethod.EXACT_IMDB_CATALOG if title_match == "exact" else MatchMethod.FUZZY_IMDB_CATALOG

            results[idx] = MatchResult(
                request=req,
                imdb_id=row["imdb_id"],
                imdb_title=row["imdb_title"],
                imdb_year=int(row["imdb_year"]) if row.get("imdb_year") else None,
                confidence=confidence,
                match_method=method,
                num_candidates=num_cand,
                metadata={"title_match": title_match, "year_match": year_match},
            )

        return results

    def _score_confidence(self, num_candidates, title_match, year_match):
        if num_candidates == 1:
            if year_match == "exact_year" and title_match == "exact":
                return Confidence.HIGH
            if year_match in ("exact_year", "year_close"):
                return Confidence.HIGH
            if year_match == "year_missing" and title_match == "exact":
                return Confidence.MEDIUM
            return Confidence.MEDIUM
        if num_candidates <= 3 and year_match == "exact_year":
            return Confidence.MEDIUM
        return Confidence.LOW
