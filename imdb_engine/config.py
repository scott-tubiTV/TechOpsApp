"""Configuration for the IMDB matching engine."""

from dataclasses import dataclass


@dataclass
class IMDBEngineConfig:
    warehouse_id: str = "a6b9541289d75c6e"
    content_info_table: str = "core_prod.tubidw.content_info"
    rich_content_table: str = "ml_prod.common.rich_content"
    imdb_catalog_table: str = "core_prod.imdb.imdb_title_essential_v2"
    policy_windows_table: str = "core_prod.policydb_cdc.content_policy_windows"
    verifications_table: str = "core_dev.techops.imdb_verifications"
    wait_timeout: str = "50s"
    poll_interval: int = 2
    poll_max_attempts: int = 30
    batch_size: int = 100
    year_tolerance: int = 1
