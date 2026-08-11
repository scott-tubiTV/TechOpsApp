"""LLM-based query decomposition — classifies and splits multi-space questions."""

import json
from dataclasses import dataclass

from llm_client import LLMClient

MODEL = "databricks-claude-haiku-4-5"

SYSTEM_PROMPT = """You are a query router for a data analytics system. You have access to the following data spaces:

1. Content Metrics — Ops metrics: policy snapshots, avails status, imports, images, partners, QA/ABF queue sizes, workload, content refresh. Questions about counts, totals, trends, queues, how many titles, monthly/weekly aggregates.
2. Redeliveries — Redelivery detail: per-title breakdown by reason, modality (video/image/subtitle), age, partner, dismissal status. Questions about redeliveries in general, overdue, backlog, dismissed, open redeliveries across the board.
3. Dupe Checker V2 — Duplicate title detection: CSV avails checking for existing titles and conflicts. Questions about dupes, duplicates, conflicts, overlapping titles, territory overlap.
4. Partner Detail — Per-partner health: error rates, redeliveries by partner, pipeline failures, ABF errors, POC info. Questions about specific partners (Sony, Paramount, NBCU, Lionsgate, etc.), partner error rates, partner health, which partners have issues.

Given a user question, determine:
- Is this a SINGLE-space question (can be fully answered by one space)?
- Or is this a MULTI-space question (requires data from multiple spaces)?

Respond ONLY with valid JSON in this exact format:
{
  "type": "single" or "multi",
  "spaces": [
    {"space_name": "<exact space name from the list above>", "sub_query": "<the question to send to this space>"}
  ],
  "reasoning": "<one sentence explaining your routing decision>"
}

Rules:
- If the question mentions a specific partner by name AND asks about their health/errors/issues, route to "Partner Detail".
- If the question asks about redeliveries in general (across all partners, or by modality/reason), route to "Redeliveries".
- If the question combines partner-specific data with general metrics or general redelivery data, it's MULTI-space.
- When splitting a multi-space question, each sub_query must be self-contained and answerable by its target space alone.
- Never route to more than 3 spaces for a single question.
- If unsure, prefer single-space routing.
- The space_name must be exactly one of: "Content Metrics", "Redeliveries", "Dupe Checker V2", "Partner Detail"."""


@dataclass
class SubQuery:
    space_name: str
    sub_query: str


@dataclass
class DecompositionResult:
    is_multi: bool
    sub_queries: list
    reasoning: str


class QueryDecomposer:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def decompose(self, question: str) -> DecompositionResult:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question[:2000]},
        ]

        response_text = self.llm.chat(model=MODEL, messages=messages, max_tokens=500)
        return self._parse_response(response_text)

    def _parse_response(self, text: str) -> DecompositionResult:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        data = json.loads(cleaned)
        valid_spaces = {"Content Metrics", "Redeliveries", "Dupe Checker V2", "Partner Detail"}

        sub_queries = []
        for space in data.get("spaces", []):
            name = space.get("space_name", "")
            if name in valid_spaces:
                sub_queries.append(SubQuery(space_name=name, sub_query=space.get("sub_query", "")))

        if not sub_queries:
            raise ValueError("No valid spaces in LLM response")

        is_multi = data.get("type") == "multi" and len(sub_queries) > 1
        return DecompositionResult(
            is_multi=is_multi,
            sub_queries=sub_queries,
            reasoning=data.get("reasoning", ""),
        )
