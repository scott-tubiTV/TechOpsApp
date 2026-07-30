"""Text normalization utilities for title matching."""

import re
import unicodedata


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_no_article(text: str) -> str:
    norm = normalize(text)
    for article in ("the ", "a ", "an "):
        if norm.startswith(article):
            return norm[len(article):]
    for article in (" the", " a", " an"):
        if norm.endswith(article):
            return norm[: -len(article)]
    return norm


def reposition_article(norm_text: str) -> str:
    for article in ("the", "a", "an"):
        if norm_text.endswith(" " + article):
            return article + " " + norm_text[: -(len(article) + 1)]
    return norm_text


def strip_parentheticals(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def strip_year_suffix(text: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", text).strip()


def generate_variants(title: str) -> list:
    """Generate all normalized variants of a title for matching."""
    variants = set()
    norm = normalize(title)
    variants.add(norm)

    no_art = normalize_no_article(title)
    variants.add(no_art)

    repo = reposition_article(norm)
    if repo != norm:
        variants.add(repo)

    stripped = normalize(strip_parentheticals(title))
    if stripped != norm:
        variants.add(stripped)
        variants.add(normalize_no_article(strip_parentheticals(title)))

    year_stripped = normalize(strip_year_suffix(title))
    if year_stripped != norm:
        variants.add(year_stripped)

    return [v for v in variants if v]


def make_normalized_key(title: str, content_type: str = "", release_year: str = "") -> str:
    """Build the global lookup key for the verification table."""
    norm = normalize(title)
    ctype = (content_type or "").upper().strip()
    year = (release_year or "").strip()
    return f"{norm}|{ctype}|{year}"


def escape_sql(val) -> str:
    if val is None:
        return "NULL"
    return str(val).replace("\\", "\\\\").replace("'", "''")
