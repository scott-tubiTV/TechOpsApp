"""Sanity bounds checker for Genie query results.

After the Genie generates SQL and returns a result, this module checks the result
against known expected ranges. If a value falls wildly outside bounds, a warning
is surfaced to the user so they don't act on bad data.

Bounds are defined per-metric keyword pattern. The checker extracts numeric values
from the result DataFrame and compares against the expected range.
"""

import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class SanityBound:
    keywords: list  # if ALL keywords appear in the question, this bound applies
    metric_name: str  # human-readable name for the warning
    min_val: float
    max_val: float
    column_hint: Optional[str] = None  # which column to check (None = first numeric)


BOUNDS = [
    SanityBound(
        keywords=["abf", "backlog"],
        metric_name="ABF backlog",
        min_val=3000,
        max_val=20000,
    ),
    SanityBound(
        keywords=["abf", "pending"],
        metric_name="ABF pending review",
        min_val=3000,
        max_val=20000,
    ),
    SanityBound(
        keywords=["abf", "queue"],
        metric_name="ABF queue",
        min_val=3000,
        max_val=20000,
    ),
    SanityBound(
        keywords=["internal", "review"],
        metric_name="Internal review queue",
        min_val=200,
        max_val=5000,
    ),
    SanityBound(
        keywords=["titles", "live"],
        metric_name="Live titles",
        min_val=30000,
        max_val=200000,
    ),
    SanityBound(
        keywords=["open", "redeliveries"],
        metric_name="Open redeliveries",
        min_val=50,
        max_val=10000,
    ),
    SanityBound(
        keywords=["avails", "this month"],
        metric_name="New avails this month",
        min_val=100,
        max_val=50000,
    ),
    SanityBound(
        keywords=["imports", "this month"],
        metric_name="Imports this month",
        min_val=100,
        max_val=30000,
    ),
    SanityBound(
        keywords=["imports", "last month"],
        metric_name="Imports last month",
        min_val=100,
        max_val=30000,
    ),
    SanityBound(
        keywords=["active", "partners"],
        metric_name="Active partners",
        min_val=50,
        max_val=500,
    ),
    SanityBound(
        keywords=["reviews", "completed", "month"],
        metric_name="Reviews completed this month",
        min_val=100,
        max_val=50000,
    ),
    SanityBound(
        keywords=["expiring", "month"],
        metric_name="Titles expiring this month",
        min_val=100,
        max_val=30000,
    ),
]


def _extract_total(df: pd.DataFrame, column_hint: Optional[str] = None) -> Optional[float]:
    """Extract the primary numeric value from a result DataFrame.

    For single-row results, return the first numeric value.
    For multi-row results, sum the first numeric column (team breakdowns, etc).
    """
    if df is None or df.empty:
        return None

    if column_hint and column_hint in df.columns:
        try:
            return pd.to_numeric(df[column_hint], errors="coerce").sum()
        except Exception:
            pass

    numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
    if not numeric_cols:
        for col in df.columns:
            try:
                vals = pd.to_numeric(df[col], errors="coerce")
                if vals.notna().any():
                    numeric_cols = [col]
                    break
            except Exception:
                continue

    if not numeric_cols:
        return None

    col = numeric_cols[0]
    vals = pd.to_numeric(df[col], errors="coerce")

    if len(df) == 1:
        return vals.iloc[0] if pd.notna(vals.iloc[0]) else None

    total = vals.sum()
    return total if pd.notna(total) else None


def check_sanity(question: str, df: pd.DataFrame) -> Optional[str]:
    """Check a Genie result against sanity bounds.

    Returns a warning string if the result is outside expected range, None otherwise.
    """
    if df is None or df.empty:
        return None

    question_lower = question.lower()

    for bound in BOUNDS:
        if all(kw in question_lower for kw in bound.keywords):
            value = _extract_total(df, bound.column_hint)
            if value is None:
                continue

            if value < bound.min_val:
                return (
                    f"⚠️ **Data check:** The reported {bound.metric_name} ({value:,.0f}) "
                    f"is unusually low (expected range: {bound.min_val:,.0f}–{bound.max_val:,.0f}). "
                    f"This may indicate the query is filtering too narrowly. Verify before acting on this number."
                )
            elif value > bound.max_val:
                return (
                    f"⚠️ **Data check:** The reported {bound.metric_name} ({value:,.0f}) "
                    f"is unusually high (expected range: {bound.min_val:,.0f}–{bound.max_val:,.0f}). "
                    f"This may indicate the query is overcounting (e.g., rows instead of unique titles). "
                    f"Verify before acting on this number."
                )
            break  # only check first matching bound

    return None
