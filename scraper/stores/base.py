from abc import ABC, abstractmethod
import logging
import random

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from scraper import config
from scraper.models import StoreResults, SearchResult
from scraper.utils.stealth import (
    apply_stealth,
    random_delay,
    human_like_interaction,
    simulate_human_behavior,
    inject_mouse_tracker,
)

logger = logging.getLogger(__name__)


class BaseStoreScraper(ABC):
    """Abstract base class for store scrapers."""

    name: str = "base"

    def __init__(self, context: BrowserContext, debug_mouse: bool = False):
        self.context = context
        self.page: Page | None = None
        self.debug_mouse = debug_mouse

    async def init_page(self) -> None:
        """Initialize a new page with stealth settings."""
        try:
            self.page = await self.context.new_page()
            await apply_stealth(self.page)
            if self.debug_mouse:
                await inject_mouse_tracker(self.page)
            self.page.set_default_timeout(config.PAGE_LOAD_TIMEOUT)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to initialize page: {e}")
            raise

    async def close_page(self) -> None:
        """Close the page if open."""
        if self.page:
            await self.page.close()
            self.page = None

    async def _check_for_block_page(self) -> StoreResults | None:
        """Check if the current page is a WAF/CDN block page.

        Returns StoreResults with status='blocked' if detected, or None if normal.
        """
        try:
            body_text = await self.page.text_content("body", timeout=3000)
            if not body_text:
                return None

            body_lower = body_text.lower()
            block_indicators = [
                "this content is blocked",
                "access denied",
                "verify you are human",
                "attention required",
                "checking your browser",
                "ray id",
            ]

            for indicator in block_indicators:
                if indicator in body_lower:
                    logger.warning(
                        f"[{self.name}] Block page detected: '{indicator}'"
                    )
                    return StoreResults(
                        status="blocked",
                        error_message=f"Block page detected: '{indicator}'",
                    )

            return None
        except Exception:
            return None

    @abstractmethod
    def get_search_url(self, query: str) -> str:
        """Return the search URL for the given query."""
        pass

    @abstractmethod
    async def parse_results(self, search_term: str) -> tuple[list[SearchResult], int]:
        """Parse search results from the current page.

        Args:
            search_term: The search query used for confidence scoring

        Returns:
            Tuple of (results list, count of tiles skipped due to missing price)
        """
        pass

    @abstractmethod
    async def wait_for_results(self) -> bool:
        """Wait for results to load. Returns True if results found."""
        pass

    async def search(self, search_term: str) -> StoreResults:
        """
        Perform a search and return results.
        Handles retries and error cases.
        """
        for attempt in range(config.RETRY_COUNT + 1):
            try:
                if not self.page:
                    await self.init_page()

                url = self.get_search_url(search_term)
                logger.info(f"[{self.name}] Searching: {search_term} (attempt {attempt + 1})")

                await human_like_interaction(self.page)
                await self.page.goto(url, wait_until="domcontentloaded")

                # Re-inject mouse tracker after navigation (DOM is replaced)
                if self.debug_mouse:
                    await inject_mouse_tracker(self.page)

                # Check for WAF/CDN block pages before waiting for results
                block_result = await self._check_for_block_page()
                if block_result:
                    return block_result

                has_results = await self.wait_for_results()

                if not has_results:
                    logger.info(f"[{self.name}] No results for: {search_term}")
                    return StoreResults(status="no_results")

                results, skipped_no_price = await self.parse_results(search_term)

                if not results:
                    return StoreResults(status="no_results", skipped_no_price=skipped_no_price)

                # Enforce max results limit (parse_results may return more)
                results = results[:config.MAX_RESULTS_PER_STORE]

                # Simulate human behavior during wait period (instead of just sleeping)
                delay_duration = random.uniform(
                    config.DELAY_BETWEEN_SEARCHES_MIN,
                    config.DELAY_BETWEEN_SEARCHES_MAX
                )
                await simulate_human_behavior(self.page, delay_duration)

                return StoreResults(status="success", results=results, skipped_no_price=skipped_no_price)

            except PlaywrightTimeout as e:
                logger.warning(f"[{self.name}] Timeout for {search_term}: {e}")
                if attempt < config.RETRY_COUNT:
                    await random_delay()
                    continue
                return StoreResults(status="error", error_message=f"Timeout: {e}")

            except Exception as e:
                logger.error(f"[{self.name}] Error for {search_term}: {e}")
                error_lower = str(e).lower()
                block_keywords = [
                    "blocked", "captcha", "access denied", "403", "rate limit",
                    "too many requests", "cloudflare", "recaptcha", "verify"
                ]
                if any(kw in error_lower for kw in block_keywords):
                    return StoreResults(status="blocked", error_message=str(e))
                if attempt < config.RETRY_COUNT:
                    await random_delay()
                    continue
                return StoreResults(status="error", error_message=str(e))

        return StoreResults(status="error", error_message="Max retries exceeded")
