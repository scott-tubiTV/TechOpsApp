"""Genie API client for interacting with Databricks Genie spaces."""

import time
import requests
from databricks.sdk import WorkspaceClient


class GenieClient:
    def __init__(self):
        self.w = WorkspaceClient()
        self.base_url = f"{self.w.config.host}/api/2.0/genie"

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.w.config.token}",
            "Content-Type": "application/json",
        }

    def start_conversation(self, space_id: str, message: str) -> dict:
        url = f"{self.base_url}/spaces/{space_id}/start-conversation"
        resp = requests.post(url, headers=self._headers(), json={"content": message})
        resp.raise_for_status()
        return resp.json()

    def get_message(self, space_id: str, conversation_id: str, message_id: str) -> dict:
        url = f"{self.base_url}/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}"
        resp = requests.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def send_followup(self, space_id: str, conversation_id: str, message: str) -> dict:
        url = f"{self.base_url}/spaces/{space_id}/conversations/{conversation_id}/messages"
        resp = requests.post(url, headers=self._headers(), json={"content": message})
        resp.raise_for_status()
        return resp.json()

    def ask(self, space_id: str, message: str, conversation_id: str = None, timeout: int = 120) -> dict:
        """Send a message and poll until completion. Returns the full message response."""
        if conversation_id:
            result = self.send_followup(space_id, conversation_id, message)
            msg_id = result.get("message_id") or result.get("id")
        else:
            result = self.start_conversation(space_id, message)
            conversation_id = result["conversation_id"]
            msg_id = result["message_id"]

        start = time.time()
        while time.time() - start < timeout:
            msg = self.get_message(space_id, conversation_id, msg_id)
            status = msg.get("status", "")
            if status in ("COMPLETED", "FAILED"):
                msg["conversation_id"] = conversation_id
                return msg
            time.sleep(2)

        return {"status": "TIMEOUT", "conversation_id": conversation_id}

    def parse_response(self, msg: dict) -> dict:
        """Extract the useful parts from a Genie message response."""
        result = {
            "status": msg.get("status"),
            "conversation_id": msg.get("conversation_id"),
            "text": None,
            "sql": None,
            "suggested_questions": [],
        }

        for attachment in msg.get("attachments", []):
            if "text" in attachment:
                result["text"] = attachment["text"].get("content")
            if "query" in attachment:
                result["sql"] = attachment["query"].get("query")
            if "suggested_questions" in attachment:
                result["suggested_questions"] = attachment["suggested_questions"].get("questions", [])

        return result
