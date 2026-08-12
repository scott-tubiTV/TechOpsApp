"""Data models for the IMDB matching engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


class Confidence(Enum):
    VERIFIED = "VERIFIED"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CONFLICT = "CONFLICT"


class MatchMethod(Enum):
    VERIFIED_LOOKUP = "verified_lookup"
    DIRECT_CONTENT_INFO = "direct_content_info"
    DIRECT_RICH_CONTENT = "direct_rich_content"
    EXACT_IMDB_CATALOG = "exact_imdb_catalog"
    FUZZY_IMDB_CATALOG = "fuzzy_imdb_catalog"
    DIRECTOR_DISAMBIGUATION = "director_disambiguation"
    CROSS_VALIDATED = "cross_validated"
    AVAILS_FALLBACK = "avails_fallback"


@dataclass
class MatchRequest:
    title: str
    content_type: str = ""
    release_year: str = ""
    director: str = ""
    language: str = ""
    import_id: str = ""
    content_id: str = ""


@dataclass
class MatchResult:
    request: MatchRequest
    imdb_id: Optional[str] = None
    imdb_title: Optional[str] = None
    imdb_year: Optional[int] = None
    content_id: Optional[str] = None
    confidence: Confidence = Confidence.LOW
    match_method: MatchMethod = MatchMethod.EXACT_IMDB_CATALOG
    alternate_imdb_id: Optional[str] = None
    num_candidates: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class VerificationRecord:
    title: str
    imdb_id: str
    verified_by: str
    content_type: str = ""
    release_year: str = ""
    content_id: str = ""
    imdb_title: str = ""
    match_method: str = ""
    verified_at: Optional[datetime] = None
    import_id: str = ""
    notes: str = ""
