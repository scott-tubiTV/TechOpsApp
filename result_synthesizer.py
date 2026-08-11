"""LLM-based synthesis of results from multiple Genie spaces."""

from dataclasses import dataclass, field

from llm_client import LLMClient

MODEL = "databricks-claude-sonnet-4-5"


@dataclass
class SpaceResult:
    space_name: str
    sub_query: str
    status: str  # COMPLETED, FAILED, TIMEOUT
    text: str = None
    sql: str = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    error: str = None


class ResultSynthesizer:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def synthesize(self, original_question: str, results: list) -> str:
        completed = [r for r in results if r.status == "COMPLETED"]
        if not completed:
            return "All queries failed. Please try rephrasing your question."

        if len(completed) == 1 and len(results) == 1:
            return completed[0].text or "Query completed but no text response was returned."

        prompt = self._build_prompt(original_question, results)
        messages = [
            {"role": "system", "content": "You are a data analyst synthesizing results from multiple data sources into one clear answer. Be concise and reference specific numbers from the data."},
            {"role": "user", "content": prompt},
        ]

        try:
            return self.llm.chat(model=MODEL, messages=messages, max_tokens=1000)
        except Exception:
            parts = []
            for r in completed:
                if r.text:
                    parts.append(f"**{r.space_name}:** {r.text}")
            return "\n\n".join(parts) if parts else "Results retrieved but synthesis unavailable."

    def _build_prompt(self, question: str, results: list) -> str:
        sections = [f'The user asked: "{question}"\n\nHere are the results from each data source:\n']

        for r in results:
            section = f"## Source: {r.space_name}\n"
            section += f"Sub-question: {r.sub_query}\n"
            section += f"Status: {r.status}\n"

            if r.status == "COMPLETED":
                if r.text:
                    section += f"Response: {r.text}\n"
                if r.columns and r.rows:
                    preview_rows = r.rows[:10]
                    section += f"Data columns: {', '.join(r.columns)}\n"
                    section += f"Data ({len(r.rows)} rows, showing first {len(preview_rows)}):\n"
                    for row in preview_rows:
                        section += f"  {row}\n"
                if r.sql:
                    section += f"SQL used: {r.sql[:300]}\n"
            else:
                section += f"Error: {r.error or 'Unknown failure'}\n"

            sections.append(section)

        sections.append(
            "\nCombine these results into one coherent answer (2-4 sentences plus relevant numbers). "
            "If a source failed, acknowledge what's missing. Do NOT fabricate data."
        )
        return "\n".join(sections)
