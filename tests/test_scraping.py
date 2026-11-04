import copy
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

from scraper import config
from scraper.stores.store_b import STORE_CONFIG, StoreBScraper
from scraper.utils.stealth import apply_stealth, create_stealth_browser, get_user_agent


EXAMPLE_CONFIG = runpy.run_path(
    str(Path(__file__).parents[1] / "scraper/stores/store_config.example.py")
)["STORE_B"]

# Sanitized structure from the new product cards. Hashes deliberately differ
# from the live page so a build-specific CSS hash cannot accidentally pass.
CARD = """
<div class="product-tile_component_product-tile__test">
  <div class="product-tile_component_container__test">
    <div class="product-tile_component_content-container__test">
      <div class="product-tile-image_image__test">
        <a href="/product/123/apples" aria-label="Example apples, $1.31 each">
          <img src="https://images.example.com/123.jpg">
        </a>
      </div>
      <div class="product-tile-price-info_lead-price__test">
        <div>$</div><div>1.31</div>
      </div>
      <div class="product-tile-price-info_cup-price__test">$1.31 / 1EA</div>
      <div class="product-tile-name_product-tile-name__test">
        <a href="/product/123/apples">Example apples each</a>
      </div>
    </div>
    <div class="product-tile_component_container__nested">Lists</div>
  </div>
</div>
"""


class StoreBTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(channel="chromium")
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.scraper = StoreBScraper(self.context)
        self.scraper.page = await self.context.new_page()
        self.config_patch = patch.dict(STORE_CONFIG, copy.deepcopy(EXAMPLE_CONFIG), clear=True)
        self.config_patch.start()

    async def asyncTearDown(self):
        self.config_patch.stop()

    async def test_extracts_new_card_and_excludes_recommendations(self):
        await self.scraper.page.set_content(
            '<div data-testid="search-results-product">' + CARD + '</div>'
            '<aside aria-label="You may also like">' + CARD + '</aside>'
        )
        self.assertTrue(await self.scraper.wait_for_results())
        results, skipped = await self.scraper.parse_results("Example apples")
        self.assertEqual(len(results), 1)
        self.assertEqual(skipped, 0)
        result = results[0]
        self.assertEqual(result.name, "Example apples each")
        self.assertEqual(result.price, "$1.31")
        self.assertEqual(result.unit_price, "$1.31 / 1EA")
        self.assertEqual(result.url, "https://store-b.example.com/product/123/apples")
        self.assertEqual(result.product_id, "123")
        self.assertEqual(result.image_url, "https://images.example.com/123.jpg")
        self.assertEqual(result.weight_info, "Example apples, $1.31 each")

    async def test_extracts_legacy_shadow_dom_variant(self):
        await self.scraper.page.set_content(
            '<div data-testid="search-results-product">'
            '<div class="product-tile_component_product-tile__legacy">'
            '<wc-product-tile></wc-product-tile></div></div>'
        )
        await self.scraper.page.locator("wc-product-tile").evaluate("""el => {
            el.attachShadow({mode: 'open'}).innerHTML = `
                <div class="title"><a href="/product/123/apples">Example apples each</a></div>
                <div class="product-tile-price"><span class="primary">$1.31</span></div>
                <span class="price-per-cup">$1.31 / 1EA</span>`;
        }""")
        with patch.object(config, "SELECTOR_TIMEOUT", 100):
            self.assertTrue(await self.scraper.wait_for_results())
        results, skipped = await self.scraper.parse_results("Example apples")
        self.assertEqual([(r.name, r.price, r.unit_price) for r in results],
                         [("Example apples each", "$1.31", "$1.31 / 1EA")])
        self.assertEqual(skipped, 0)

    async def test_selector_timeout_is_an_error_not_empty_results(self):
        await self.scraper.page.set_content("<p>Products rendered with unknown markup</p>")
        with patch.object(config, "SELECTOR_TIMEOUT", 100):
            with self.assertRaises(PlaywrightTimeout):
                await self.scraper.wait_for_results()

    async def test_missing_name_is_an_extraction_error(self):
        await self.scraper.page.set_content(
            '<div data-testid="search-results-product">' +
            CARD.replace("product-tile-name_product-tile-name__test", "changed-name-markup") + '</div>'
        )
        with self.assertLogs("scraper.stores.store_b", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "extract"):
                await self.scraper.parse_results("Example apples")

    async def test_unparseable_price_is_an_extraction_error(self):
        await self.scraper.page.set_content(
            '<div data-testid="search-results-product">' +
            CARD.replace("<div>$</div><div>1.31</div>", "Price format changed") + '</div>'
        )
        with self.assertLogs("scraper.stores.store_b", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "extract"):
                await self.scraper.parse_results("Example apples")

    async def test_out_of_stock_card_without_price_is_skipped(self):
        await self.scraper.page.set_content(
            '<div data-testid="search-results-product">' +
            CARD.replace("product-tile-price-info_lead-price__test", "unpriced") + CARD + '</div>'
        )
        results, skipped = await self.scraper.parse_results("Example apples")
        self.assertEqual(len(results), 1)
        self.assertEqual(skipped, 1)

    async def test_search_retries_selector_failure_and_records_error(self):
        await self.scraper.page.route("**/*", lambda route: route.fulfill(body="<p>Unknown markup</p>"))
        with patch.object(config, "SELECTOR_TIMEOUT", 100), \
                patch("scraper.stores.base.random_delay", new_callable=AsyncMock), \
                patch("scraper.stores.base.human_like_interaction", new_callable=AsyncMock):
            with self.assertLogs("scraper.stores.base", level="WARNING"):
                result = await self.scraper.search("apples")
        self.assertEqual(result.status, "error")
        self.assertIn("Timeout", result.error_message)

    async def test_explicit_empty_results_ignores_recommended_products(self):
        await self.scraper.page.set_content(
            '<div class="zero-results">No products found</div>' + CARD
        )
        self.assertFalse(await self.scraper.wait_for_results())

    async def test_hidden_empty_message_does_not_override_results(self):
        await self.scraper.page.set_content(
            '<div class="zero-results" style="display:none">No products found</div>'
            '<div data-testid="search-results-product">' + CARD + '</div>'
        )
        self.assertTrue(await self.scraper.wait_for_results())

    async def test_block_page_is_not_empty_results(self):
        await self.scraper.page.set_content("<p>Access Denied</p>")
        with patch.object(config, "SELECTOR_TIMEOUT", 100):
            from scraper.models import BlockedError
            with self.assertRaises(BlockedError):
                await self.scraper.wait_for_results()


class PriceAndUserAgentTests(unittest.TestCase):
    def test_split_currency_is_joined_without_taking_previous_price(self):
        scraper = StoreBScraper(None)
        for text in ["$\n1.31", "$1.31", "$\n 1.31\nWas $2.00"]:
            with self.subTest(text=text):
                self.assertEqual(scraper._clean_price(text), "$1.31")

    def test_grouped_price_is_safe_for_processor(self):
        self.assertEqual(StoreBScraper(None)._clean_price("$1,234.56"), "$1234.56")

    def test_user_agent_preserves_native_platform_and_engine(self):
        native = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36"
        self.assertEqual(get_user_agent(native), native.replace("HeadlessChrome/", "Chrome/"))
        self.assertEqual(get_user_agent(native.replace("HeadlessChrome/", "Chrome/")), native.replace("HeadlessChrome/", "Chrome/"))


class BrowserIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ua_headers_client_hints_and_locale_match_engine(self):
        playwright, browser, context = await create_stealth_browser(headless=True, locale="en-AU", timezone_id="Australia/Sydney")
        try:
            page = await context.new_page()
            await apply_stealth(page)
            await page.route("**/*", lambda route: route.fulfill(body="<p>Browser identity</p>"))
            response = await page.goto("https://browser-test.example/")
            headers = await response.request.all_headers()
            identity = await page.evaluate("""() => ({
                ua: navigator.userAgent,
                brands: navigator.userAgentData.brands,
                languages: navigator.languages,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
            })""")
            major = browser.version.split(".")[0]
            self.assertEqual(headers["user-agent"], identity["ua"])
            self.assertIn(f"Chrome/{major}.", identity["ua"])
            self.assertNotIn("HeadlessChrome", identity["ua"])
            self.assertIn({"brand": "Chromium", "version": major}, identity["brands"])
            self.assertIn(f'"Chromium";v="{major}"', headers["sec-ch-ua"])
            self.assertEqual(identity["languages"][0], "en-AU")
            self.assertEqual(identity["timezone"], "Australia/Sydney")
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()


class WizardLogTests(unittest.TestCase):
    def test_successful_subprocess_preserves_and_surfaces_warnings(self):
        from scraper.wizard.steps.progress import run_scraper_subprocess

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            result_file = output_dir / "results_test.json"
            result_file.write_text('{"items": []}', encoding="utf-8")

            def start_process(*args, **kwargs):
                kwargs["stderr"].write("2026-11-04 - WARNING - selector failed\n")
                process = unittest.mock.Mock()
                process.poll.return_value = 0
                process.returncode = 0
                return process

            with patch("scraper.wizard.steps.progress.subprocess.Popen", side_effect=start_process):
                with self.assertLogs("scraper.wizard.steps.progress", level="WARNING") as logs:
                    self.assertEqual(run_scraper_subprocess(Path("input.csv"), output_dir, "test"), result_file)
            self.assertIn("selector failed", "\n".join(logs.output))
            self.assertIn("selector failed", (output_dir / "scraper_test.log").read_text(encoding="utf-8"))


class StoreCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_with_zero_successes_emits_warning(self):
        from scraper.main import scrape_store
        from scraper.models import InputItem, StoreResults
        scraper = unittest.mock.Mock(name="scraper")
        scraper.name = "Example store"
        scraper.init_page = AsyncMock()
        scraper.close_page = AsyncMock()
        scraper.search = AsyncMock(return_value=StoreResults(status="no_results"))
        with self.assertLogs("scraper.main", level="WARNING") as logs:
            await scrape_store(scraper, [InputItem(1, 2, "apples", 100, "apples")])
        self.assertIn("no successful", "\n".join(logs.output).lower())


if __name__ == "__main__":
    unittest.main()
