"""Thin wrapper around Databricks Foundation Model serving endpoints."""

from databricks.sdk import WorkspaceClient


class LLMClient:
    def __init__(self, workspace_client: WorkspaceClient = None):
        self.w = workspace_client or WorkspaceClient()

    def chat(self, model: str, messages: list, max_tokens: int = 1000, temperature: float = 0.0) -> str:
        try:
            response = self.w.serving_endpoints.query(
                name=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content
        except (AttributeError, TypeError):
            response = self.w.api_client.do(
                "POST",
                f"/serving-endpoints/{model}/invocations",
                body={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            )
            return response["choices"][0]["message"]["content"]
