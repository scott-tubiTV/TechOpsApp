"""Verification table CRUD — the learning system."""

from datetime import datetime, timezone

from .config import IMDBEngineConfig
from .models import MatchRequest, VerificationRecord
from .normalizer import escape_sql, make_normalized_key
from .sql_client import SQLClient


class IMDBVerifier:
    def __init__(self, sql_client: SQLClient = None, config: IMDBEngineConfig = None):
        self.config = config or IMDBEngineConfig()
        self.sql = sql_client or SQLClient(config=self.config)
        self._table = self.config.verifications_table

    def ensure_table_exists(self):
        try:
            self.sql.execute(f"SELECT 1 FROM {self._table} LIMIT 1")
            return
        except Exception:
            pass
        self.sql.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                normalized_key     STRING NOT NULL,
                title              STRING NOT NULL,
                content_type       STRING,
                release_year       STRING,
                content_id         STRING,
                imdb_id            STRING NOT NULL,
                imdb_title         STRING,
                match_method       STRING,
                verified_by        STRING NOT NULL,
                verified_at        TIMESTAMP NOT NULL,
                import_id          STRING,
                notes              STRING
            ) USING DELTA
        """)

    def record_verification(self, record: VerificationRecord):
        now = (record.verified_at or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        norm_key = make_normalized_key(
            record.title, record.content_type, record.release_year
        )
        self.sql.execute(f"""
            MERGE INTO {self._table} AS target
            USING (SELECT '{escape_sql(norm_key)}' AS normalized_key) AS source
            ON target.normalized_key = source.normalized_key
            WHEN MATCHED THEN UPDATE SET
                imdb_id = '{escape_sql(record.imdb_id)}',
                imdb_title = '{escape_sql(record.imdb_title)}',
                content_id = '{escape_sql(record.content_id)}',
                match_method = '{escape_sql(record.match_method)}',
                verified_by = '{escape_sql(record.verified_by)}',
                verified_at = '{now}',
                import_id = '{escape_sql(record.import_id)}',
                notes = '{escape_sql(record.notes)}'
            WHEN NOT MATCHED THEN INSERT
                (normalized_key, title, content_type, release_year, content_id,
                 imdb_id, imdb_title, match_method, verified_by, verified_at, import_id, notes)
            VALUES
                ('{escape_sql(norm_key)}', '{escape_sql(record.title)}',
                 '{escape_sql(record.content_type)}', '{escape_sql(record.release_year)}',
                 '{escape_sql(record.content_id)}', '{escape_sql(record.imdb_id)}',
                 '{escape_sql(record.imdb_title)}', '{escape_sql(record.match_method)}',
                 '{escape_sql(record.verified_by)}', '{now}',
                 '{escape_sql(record.import_id)}', '{escape_sql(record.notes)}')
        """)

    def lookup_verified(self, requests: list) -> dict:
        """Batch lookup. Returns {normalized_key: row_dict} for found verifications."""
        if not requests:
            return {}

        keys = []
        for req in requests:
            key = make_normalized_key(req.title, req.content_type, req.release_year)
            keys.append(key)

        unique_keys = list(set(keys))
        results = {}

        for i in range(0, len(unique_keys), self.config.batch_size):
            chunk = unique_keys[i : i + self.config.batch_size]
            key_list = ", ".join(f"'{escape_sql(k)}'" for k in chunk)
            rows = self.sql.execute(f"""
                SELECT normalized_key, title, content_type, release_year,
                       content_id, imdb_id, imdb_title, match_method
                FROM {self._table}
                WHERE normalized_key IN ({key_list})
            """)
            for row in rows:
                results[row["normalized_key"]] = row

        return results

    def get_stats(self) -> dict:
        rows = self.sql.execute(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(DISTINCT imdb_id) AS unique_imdb_ids,
                COUNT(DISTINCT content_id) AS unique_content_ids
            FROM {self._table}
        """)
        return rows[0] if rows else {}
