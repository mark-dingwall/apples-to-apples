from dataclasses import dataclass, field
from typing import Literal


class BlockedError(Exception):
    """Raised when a store blocks the scraper."""

    def __init__(self, store: str, message: str = ""):
        self.store = store
        self.message = message or f"Blocked by {store}"
        super().__init__(self.message)


@dataclass
class OfferPart:
    """An item from the database."""

    id: int
    name: str
    price: int  # cents
    category_id: int


@dataclass
class InputItem:
    id: int
    category_id: int
    name: str
    price_cents: int
    extracted_search_term: str = ""


@dataclass
class SearchResult:
    rank: int
    name: str
    price: str
    unit_price: str
    url: str
    is_on_special: bool
    is_available: bool
    confidence_score: float
    html_snippet: str
    product_id: str = ""
    image_url: str = ""
    weight_info: str = ""

    def __post_init__(self):
        if self.rank < 1:
            raise ValueError(f"rank must be positive, got {self.rank}")
        if not (0.0 <= self.confidence_score <= 1.0):
            raise ValueError(f"confidence_score must be 0.0-1.0, got {self.confidence_score}")


_VALID_STATUSES = {"success", "no_results", "blocked", "error", "skipped"}


@dataclass
class StoreResults:
    status: Literal["success", "no_results", "blocked", "error", "skipped"]
    results: list[SearchResult] = field(default_factory=list)
    error_message: str = ""
    skipped_no_price: int = 0  # Count of tiles skipped due to missing price

    def __post_init__(self):
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status: {self.status}")
        if self.status == "error" and not self.error_message:
            raise ValueError("error_message must be non-empty when status is 'error'")


@dataclass
class ItemResults:
    input: InputItem
    store_a: StoreResults
    store_b: StoreResults


@dataclass
class RunSummary:
    total_items: int
    store_a_success: int
    store_b_success: int


@dataclass
class ScrapeRun:
    run_timestamp: str
    items: list[ItemResults]
    summary: RunSummary
