"""Shared SQL execution layer for the IMDB engine."""

import time

from .config import IMDBEngineConfig


class SQLClient:
    """Databricks SQL Statements API client. No Streamlit dependency."""

    def __init__(self, workspace_client=None, config: IMDBEngineConfig = None):
        self.config = config or IMDBEngineConfig()
        self._workspace_client = workspace_client

    @property
    def _client(self):
        if self._workspace_client is None:
            from databricks.sdk import WorkspaceClient
            self._workspace_client = WorkspaceClient()
        return self._workspace_client

    def execute(self, query: str) -> list:
        result = self._client.api_client.do(
            "POST",
            "/api/2.0/sql/statements",
            body={
                "statement": query,
                "warehouse_id": self.config.warehouse_id,
                "wait_timeout": self.config.wait_timeout,
            },
        )

        status = result.get("status", {}).get("state")
        if status in ("PENDING", "RUNNING"):
            stmt_id = result.get("statement_id")
            for _ in range(self.config.poll_max_attempts):
                time.sleep(self.config.poll_interval)
                result = self._client.api_client.do(
                    "GET", f"/api/2.0/sql/statements/{stmt_id}"
                )
                status = result.get("status", {}).get("state")
                if status not in ("PENDING", "RUNNING"):
                    break

        if status != "SUCCEEDED":
            error = (
                result.get("status", {}).get("error", {}).get("message", "Unknown error")
            )
            raise RuntimeError(f"Query failed ({status}): {error}")

        columns = [
            c["name"]
            for c in result.get("manifest", {}).get("schema", {}).get("columns", [])
        ]
        rows = result.get("result", {}).get("data_array", [])
        return [dict(zip(columns, row)) for row in rows]
