"""Genie API client for interacting with Databricks Genie spaces."""

import time
from databricks.sdk import WorkspaceClient


class GenieClient:
    def __init__(self):
        self.w = WorkspaceClient()

    def _do(self, method, path, body=None):
        """Use the SDK's authenticated API client for all requests."""
        return self.w.api_client.do(method, path, body=body)

    def start_conversation(self, space_id: str, message: str) -> dict:
        return self._do(
            "POST",
            f"/api/2.0/genie/spaces/{space_id}/start-conversation",
            body={"content": message},
        )

    def get_message(self, space_id: str, conversation_id: str, message_id: str) -> dict:
        return self._do(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}",
        )

    def send_followup(self, space_id: str, conversation_id: str, message: str) -> dict:
        return self._do(
            "POST",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages",
            body={"content": message},
        )

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
        last_status = None
        while time.time() - start < timeout:
            msg = self.get_message(space_id, conversation_id, msg_id)
            status = msg.get("status", "")
            last_status = status
            if status in ("COMPLETED", "COMPLETED_WITH_ERROR", "FAILED"):
                msg["conversation_id"] = conversation_id
                msg["_message_id"] = msg_id
                return msg
            if status == "ASKING_AI":
                time.sleep(3)
                msg2 = self.get_message(space_id, conversation_id, msg_id)
                status2 = msg2.get("status", "")
                if status2 == "ASKING_AI":
                    msg2["conversation_id"] = conversation_id
                    msg2["_message_id"] = msg_id
                    return msg2
                elif status2 in ("COMPLETED", "COMPLETED_WITH_ERROR", "FAILED"):
                    msg2["conversation_id"] = conversation_id
                    msg2["_message_id"] = msg_id
                    return msg2
            time.sleep(2)

        return {"status": "TIMEOUT", "conversation_id": conversation_id, "_message_id": msg_id, "_last_status": last_status}

    def get_query_result(self, space_id: str, conversation_id: str, message_id: str, attachment_id: str) -> dict:
        return self._do(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/query-result/{attachment_id}",
        )

    def parse_response(self, msg: dict) -> dict:
        """Extract the useful parts from a Genie message response."""
        raw_status = msg.get("status")
        normalized_status = "COMPLETED" if raw_status in ("COMPLETED", "ASKING_AI", "COMPLETED_WITH_ERROR") else raw_status
        result = {
            "status": normalized_status,
            "raw_status": raw_status,
            "conversation_id": msg.get("conversation_id"),
            "text": None,
            "sql": None,
            "query_result": None,
            "query_description": None,
            "error": None,
            "suggested_questions": [],
        }

        for attachment in msg.get("attachments", []):
            if "text" in attachment:
                result["text"] = attachment["text"].get("content")
            if "query" in attachment:
                query_info = attachment["query"]
                result["sql"] = query_info.get("query")
                result["query_description"] = query_info.get("description")
                if query_info.get("error"):
                    result["error"] = query_info["error"]
                # Store attachment ID — could be at attachment level or in query
                att_id = attachment.get("id") or query_info.get("id")
                if att_id:
                    result["_query_attachment_id"] = att_id
            if "suggested_questions" in attachment:
                result["suggested_questions"] = attachment["suggested_questions"].get("questions", [])

        # Store raw attachments for debugging
        result["_raw_attachments"] = msg.get("attachments", [])

        if raw_status == "FAILED" and not result["text"] and not result["error"]:
            result["error"] = msg.get("error", {}).get("message") or f"Genie returned FAILED status. Raw: {str(msg.get('error', ''))}"

        return result
