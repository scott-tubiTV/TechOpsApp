"""IMDB Matching Engine — reusable title-to-IMDB matching with verification learning."""

from .config import IMDBEngineConfig
from .matcher import IMDBMatcher
from .models import Confidence, MatchMethod, MatchRequest, MatchResult, VerificationRecord
from .sql_client import SQLClient
from .verifier import IMDBVerifier

__all__ = [
    "IMDBEngineConfig",
    "IMDBMatcher",
    "IMDBVerifier",
    "SQLClient",
    "MatchRequest",
    "MatchResult",
    "VerificationRecord",
    "Confidence",
    "MatchMethod",
]
