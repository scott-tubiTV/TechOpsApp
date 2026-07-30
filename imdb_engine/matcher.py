"""Orchestrator — runs strategies in order, cross-validates results."""

from .config import IMDBEngineConfig
from .models import Confidence, MatchMethod, MatchRequest, MatchResult
from .sql_client import SQLClient
from .strategies import CatalogMatchStrategy, DirectLookupStrategy, VerifiedLookupStrategy
from .verifier import IMDBVerifier


class IMDBMatcher:
    """Main entry point. Matches a batch of titles to IMDB IDs."""

    def __init__(self, config: IMDBEngineConfig = None, sql_client: SQLClient = None):
        self.config = config or IMDBEngineConfig()
        self.sql = sql_client or SQLClient(config=self.config)
        self.verifier = IMDBVerifier(sql_client=self.sql, config=self.config)

        self._verified_strategy = VerifiedLookupStrategy(self.config)
        self._direct_strategy = DirectLookupStrategy(self.config)
        self._catalog_strategy = CatalogMatchStrategy(self.config)

    def match(self, requests: list, progress_callback=None) -> list:
        """Match a batch of MatchRequests. Returns list of MatchResults (same order)."""
        if not requests:
            return []

        results = [None] * len(requests)

        # Step 1: Verified lookup (instant, always wins)
        if progress_callback:
            progress_callback("Checking verified matches...")
        verified_lookup = self.verifier.lookup_verified(requests)
        verified_hits = self._verified_strategy.match(requests, self.sql, verified_lookup)
        for idx, result in verified_hits.items():
            results[idx] = result

        # Collect unresolved indices
        unresolved = [i for i in range(len(requests)) if results[i] is None]
        if not unresolved:
            return results

        unresolved_requests = [requests[i] for i in unresolved]

        # Step 2 & 3: Run direct lookup and catalog match in tandem
        if progress_callback:
            progress_callback(f"Matching {len(unresolved_requests)} titles against content database...")
        direct_results = self._direct_strategy.match(unresolved_requests, self.sql)

        if progress_callback:
            progress_callback(f"Matching against IMDB catalog...")
        catalog_results = self._catalog_strategy.match(unresolved_requests, self.sql)

        # Cross-validate and merge
        for local_idx in range(len(unresolved_requests)):
            global_idx = unresolved[local_idx]
            req = unresolved_requests[local_idx]

            direct = direct_results.get(local_idx)
            catalog = catalog_results.get(local_idx)

            result = self._cross_validate(req, direct, catalog)
            results[global_idx] = result

        # Fill any remaining gaps
        for i in range(len(results)):
            if results[i] is None:
                results[i] = MatchResult(request=requests[i])

        if progress_callback:
            progress_callback("Done.")

        return results

    def match_single(self, request: MatchRequest) -> MatchResult:
        return self.match([request])[0]

    def _cross_validate(self, request, direct_result, catalog_result):
        """Merge direct and catalog results with cross-validation."""
        direct_imdb = None
        catalog_imdb = None

        if direct_result and direct_result.imdb_id:
            if not direct_result.metadata.get("no_imdb"):
                direct_imdb = direct_result.imdb_id

        if catalog_result and catalog_result.imdb_id:
            catalog_imdb = catalog_result.imdb_id

        # Both paths found IMDB IDs
        if direct_imdb and catalog_imdb:
            if direct_imdb == catalog_imdb:
                # Agreement — boost to HIGH cross-validated
                best = direct_result if direct_result.confidence.value <= catalog_result.confidence.value else catalog_result
                return MatchResult(
                    request=request,
                    imdb_id=direct_imdb,
                    imdb_title=catalog_result.imdb_title or direct_result.imdb_title,
                    imdb_year=catalog_result.imdb_year,
                    content_id=direct_result.content_id,
                    confidence=Confidence.HIGH,
                    match_method=MatchMethod.CROSS_VALIDATED,
                    num_candidates=catalog_result.num_candidates,
                    metadata={"direct_method": direct_result.match_method.value,
                              "catalog_method": catalog_result.match_method.value},
                )
            else:
                # Disagreement — flag as CONFLICT
                return MatchResult(
                    request=request,
                    imdb_id=direct_imdb,
                    alternate_imdb_id=catalog_imdb,
                    content_id=direct_result.content_id,
                    confidence=Confidence.CONFLICT,
                    match_method=direct_result.match_method,
                    metadata={"direct_imdb": direct_imdb, "catalog_imdb": catalog_imdb},
                )

        # Only direct found an IMDB ID
        if direct_imdb:
            return direct_result

        # Only catalog found an IMDB ID
        if catalog_imdb:
            result = catalog_result
            # Attach content_id from direct if available
            if direct_result and direct_result.content_id:
                result.content_id = direct_result.content_id
            return result

        # Neither found IMDB, but direct may have found content_id
        if direct_result and direct_result.content_id:
            return MatchResult(
                request=request,
                content_id=direct_result.content_id,
                confidence=Confidence.LOW,
                match_method=MatchMethod.DIRECT_CONTENT_INFO,
                metadata=direct_result.metadata,
            )

        # No match at all
        return MatchResult(request=request)
