"""Wizard state management."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from scraper.db import OfferPart
from scraper.tui import UpdateRow


@dataclass
class CacheInfo:
    """Information about cached files from a previous run."""

    csv_path: Path | None = None
    results_path: Path | None = None
    timestamp: datetime | None = None


@dataclass
class WizardState:
    """State passed between wizard steps."""

    # Step 1: Offer selection
    offer_id: int | None = None
    item_count: int = 0

    # Step 2: Cache check
    cache: CacheInfo = field(default_factory=CacheInfo)
    use_cached_csv: bool = False
    use_cached_results: bool = False

    # Step 3: Options
    headless: bool = False
    limit: int | None = None
    batch_size: int = 10
    search_term_batch_size: int = 200

    # Step 4: Progress tracking
    items: list[OfferPart] = field(default_factory=list)
    search_terms: dict[int, str] = field(default_factory=dict)
    search_term_weights: dict[int, float | None] = field(default_factory=dict)
    search_term_per_qtys: dict[int, float | None] = field(default_factory=dict)
    results_path: Path | None = None
    comparison_csv_path: Path | None = None
    temp_csv_path: Path | None = None

    # Step 5: Approval
    updates: list[UpdateRow] = field(default_factory=list)
    approved: list[UpdateRow] = field(default_factory=list)

    # Step 6: Report
    executed: bool = False
    audit_log_path: Path | None = None
    html_report_path: Path | None = None

    # Timestamps
    run_timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d_%H%M%S")
    )

    def reset_for_new_run(self) -> None:
        """Reset state for a fresh run (keeps offer_id and options)."""
        self.cache = CacheInfo()
        self.use_cached_csv = False
        self.use_cached_results = False
        self.items = []
        self.search_terms = {}
        self.search_term_weights = {}
        self.search_term_per_qtys = {}
        self.results_path = None
        self.comparison_csv_path = None
        self.temp_csv_path = None
        self.updates = []
        self.approved = []
        self.executed = False
        self.audit_log_path = None
        self.html_report_path = None
        self.run_timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
