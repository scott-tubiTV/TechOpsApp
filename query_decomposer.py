"""LLM-based query decomposition — classifies and splits multi-space questions."""

import json
from dataclasses import dataclass

from llm_client import LLMClient

MODEL = "databricks-claude-haiku-4-5"

SYSTEM_PROMPT = """You are a query router for a data analytics system. You have access to the following data spaces:

1. Backlog — The content pipeline backlog from DSA's CDP Backlog Data Model, one row per title (movie or episode) per daily snapshot. Pipeline stage (ABF Incomplete, ABF Complete, In Refresh, Ready to Publish, Published), ABF review queue, metadata status, refresh status, policy windows (US and non-US), blocking gates, deals and fees, launch failures, publish readiness, days in pipeline, creator content. Questions about the backlog, what is in the pipeline, how many titles are at a stage, what is blocked, what is ready to publish, pipeline-side deal fees, launch failures.
2. Acquisitions — Content acquisition deals from the Partnerships team's Airtable export (refreshed weekly, latest state only, no history): which titles are licensed, license windows and expirations, territories, exclusivity, fees and spend, budget category (Library, Shiny, Original, Elite, Niche), strategic vertical, CAP lead, deal type, renewals. Questions about what a deal costs, total spend or fees, what is expiring or up for renewal, exclusivity, budget category or strategic vertical.
3. Redeliveries — Redelivery detail: per-title breakdown by reason, modality (video/image/subtitle), age, partner, dismissal status. Questions about redeliveries in general, overdue redeliveries, dismissed or open redeliveries, redeliveries by reason or modality.

Given a user question, determine:
- Is this a SINGLE-space question (can be fully answered by one space)?
- Or is this a MULTI-space question (requires data from more than one space)?

Respond ONLY with valid JSON in this exact format:
{
  "type": "single" or "multi",
  "spaces": [
    {"space_name": "<exact space name from the list above>", "sub_query": "<the question to send to this space>"}
  ],
  "reasoning": "<one sentence explaining your routing decision>"
}

Rules:
- Questions about the pipeline, backlog, ABF queue, metadata, refresh, policy windows, launch failures or publish readiness go to "Backlog".
- Questions about license windows or expirations, fees, spend, cost, exclusivity, budget category, strategic vertical, CAP lead, deal type or renewals go to "Acquisitions". Fee and spend questions go to "Acquisitions" unless the user explicitly asks about the pipeline or backlog.
- Questions about redeliveries (reason, modality, age, dismissed, overdue, per-partner redelivery detail) go to "Redeliveries". A bare count of titles that have an open redelivery can be answered by "Backlog", but anything about the redeliveries themselves goes to "Redeliveries".
- If the question combines data from different spaces (for example pipeline stage with acquisition fees, or backlog with redelivery detail), it is MULTI-space.
- When splitting a multi-space question, each sub_query must be self-contained and answerable by its target space alone.
- If unsure, prefer single-space routing, and prefer "Backlog".
- The space_name must be exactly one of: "Backlog", "Acquisitions", "Redeliveries"."""


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
        valid_spaces = {"Backlog", "Acquisitions", "Redeliveries"}

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
