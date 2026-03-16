"""
Out-of-stock detection heuristics and false positive tracking.
"""

import logging
from dataclasses import dataclass, field
from playwright.async_api import Locator

try:
    from scraper.stores.store_config import STORE_A, STORE_B
except ImportError:
    raise ImportError(
        "Store config not found. Copy scraper/stores/store_config.example.py "
        "to scraper/stores/store_config.py and fill in your store details."
    )

logger = logging.getLogger(__name__)


# OOS text patterns to search for (case-insensitive)
OOS_TEXT_PATTERNS = [
    "out of stock",
    "sold out",
    "currently unavailable",
    "unavailable",
    "not available",
    "temporarily unavailable",
]


@dataclass
class OOSTrigger:
    """Represents a triggered OOS heuristic."""
    category: str  # "text", "selector", "empty_price", "disabled_button"
    detail: str    # Specific pattern or element that triggered
    confidence: str  # "high", "medium", "low"

    def __str__(self) -> str:
        return f"{self.category}:{self.detail}"


class FalsePositiveTracker:
    """Tracks patterns marked as false positives to avoid repeated prompts."""

    def __init__(self):
        self.suppressed_triggers: set[str] = set()
        self.suppressed_product_ids: set[str] = set()

    def suppress(self, triggers: list[OOSTrigger], product_id: str | None = None):
        """Mark triggers as false positives."""
        for trigger in triggers:
            self.suppressed_triggers.add(str(trigger))
        if product_id:
            self.suppressed_product_ids.add(product_id)

    def is_suppressed(self, triggers: list[OOSTrigger], product_id: str | None = None) -> bool:
        """Check if triggers should be suppressed.

        Returns True if:
        - The product_id has been explicitly suppressed, OR
        - ALL triggers in the list have been marked as false positives
        """
        if product_id and product_id in self.suppressed_product_ids:
            return True
        # Empty list means nothing to suppress - return False (all([]) returns True!)
        if not triggers:
            return False
        # Suppressed only if ALL triggers have been marked as false positives
        return all(str(t) in self.suppressed_triggers for t in triggers)


class OOSHeuristics:
    """Detects potential out-of-stock items using various heuristics."""

    def __init__(self, store: str):
        self.store = store
        self.cfg = STORE_A if store == "store_a" else STORE_B
        self.baseline_html: str | None = None

    def set_baseline(self, tile_html: str):
        """Store baseline HTML from a known in-stock item."""
        self.baseline_html = tile_html

    async def check_tile(self, tile: Locator) -> list[OOSTrigger]:
        """
        Check a product tile for OOS indicators.
        Returns list of triggered heuristics (empty if likely in-stock).
        """
        triggers = []

        # Get tile text content for text-based checks
        try:
            text = await tile.text_content() or ""
            text_lower = text.lower()
        except Exception as e:
            logger.debug(f"[{self.store}] Error getting tile text: {e}")
            text_lower = ""

        # HIGH CONFIDENCE: Explicit OOS text patterns
        for pattern in OOS_TEXT_PATTERNS:
            if pattern in text_lower:
                triggers.append(OOSTrigger(
                    category="text",
                    detail=pattern,
                    confidence="high"
                ))

        # HIGH CONFIDENCE: Existing unavailable selectors
        unavailable_selector = self.cfg["selectors"]["unavailable"]
        try:
            unavailable_locator = tile.locator(unavailable_selector)
            if await unavailable_locator.count() > 0:
                triggers.append(OOSTrigger(
                    category="selector",
                    detail=unavailable_selector,
                    confidence="high"
                ))
        except Exception as e:
            logger.debug(f"[{self.store}] Error checking unavailable selector: {e}")

        # MEDIUM CONFIDENCE: Empty/missing price
        price_selector = self.cfg["selectors"]["product_price"]
        try:
            price_locator = tile.locator(price_selector)
            if await price_locator.count() == 0:
                triggers.append(OOSTrigger(
                    category="empty_price",
                    detail="no_price_element",
                    confidence="medium"
                ))
            else:
                price_text = await price_locator.first.text_content() or ""
                if not price_text.strip() or "$" not in price_text:
                    triggers.append(OOSTrigger(
                        category="empty_price",
                        detail="price_missing_value",
                        confidence="medium"
                    ))
        except Exception as e:
            logger.debug(f"[{self.store}] Error checking price selector: {e}")

        # MEDIUM CONFIDENCE: Disabled/missing add-to-cart button
        button_selector = self.cfg.get("add_button_selector", "")
        if button_selector:
            try:
                button_locator = tile.locator(button_selector)
                if await button_locator.count() > 0:
                    # Check if button is disabled
                    is_disabled = await button_locator.first.is_disabled()
                    if is_disabled:
                        triggers.append(OOSTrigger(
                            category="disabled_button",
                            detail="add_to_cart_disabled",
                            confidence="medium"
                        ))
            except Exception as e:
                logger.debug(f"[{self.store}] Error checking button selector: {e}")

        return triggers

    def format_triggers(self, triggers: list[OOSTrigger]) -> str:
        """Format triggers for display."""
        if not triggers:
            return "  (none)"

        lines = []
        for t in triggers:
            lines.append(f"  - [{t.confidence}] {t.category}: {t.detail}")
        return "\n".join(lines)
