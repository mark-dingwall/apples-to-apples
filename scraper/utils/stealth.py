import asyncio
import logging
import random
import time

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
from playwright_stealth import Stealth

from scraper import config

logger = logging.getLogger(__name__)


def get_random_user_agent() -> str:
    """Select a random user agent from the configured pool."""
    return random.choice(config.USER_AGENTS)


async def create_stealth_browser(
    headless: bool = False,
    proxy: str | None = None,
    window_bounds: tuple[int, int, int, int] | None = None,
    locale: str = "en-US",
    timezone_id: str = "UTC",
) -> tuple[Playwright, Browser, BrowserContext]:
    """
    Create a browser instance with stealth settings to avoid detection.

    Args:
        headless: Run browser in headless mode
        proxy: Optional proxy URL (e.g., "http://proxy:8080" or "socks5://proxy:1080")
        window_bounds: Optional (x, y, width, height) for window positioning

    Returns:
        (playwright, browser, context) tuple. Caller must close all three.
    """
    playwright = await async_playwright().start()

    # Build launch options
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]

    # Position/size the browser window if bounds provided and not headless
    if window_bounds and not headless:
        x, y, w, h = window_bounds
        launch_args.append(f"--window-position={x},{y}")
        launch_args.append(f"--window-size={w},{h}")

    launch_kwargs: dict = {
        "headless": headless,
        "args": launch_args,
    }

    # Add proxy if provided
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
        logger.info(f"Using proxy: {proxy}")

    browser = await playwright.chromium.launch(**launch_kwargs)

    # Select random user agent for this session
    user_agent = get_random_user_agent()
    logger.debug(f"Using user agent: {user_agent[:50]}...")

    # Adjust viewport to match window bounds (account for browser chrome)
    vp_width = config.VIEWPORT_WIDTH
    vp_height = config.VIEWPORT_HEIGHT
    if window_bounds and not headless:
        _, _, w, h = window_bounds
        vp_width = w
        vp_height = max(400, h - 80)

    context = await browser.new_context(
        viewport={"width": vp_width, "height": vp_height},
        user_agent=user_agent,
        locale=locale,
        timezone_id=timezone_id,
    )

    return playwright, browser, context


async def apply_stealth(page: Page) -> None:
    """Apply stealth settings to a page."""
    await Stealth().apply_stealth_async(page)


async def random_delay(min_seconds: float | None = None, max_seconds: float | None = None) -> None:
    """Sleep for a random duration between min and max seconds."""
    if min_seconds is None:
        min_seconds = config.DELAY_BETWEEN_SEARCHES_MIN
    if max_seconds is None:
        max_seconds = config.DELAY_BETWEEN_SEARCHES_MAX

    delay = random.uniform(min_seconds, max_seconds)
    await asyncio.sleep(delay)


def _ease_in_out(t: float) -> float:
    """Smoothstep easing function: slow start, fast middle, slow end."""
    return t * t * (3 - 2 * t)


def _quadratic_bezier(t: float, p0: float, p1: float, p2: float) -> float:
    """Quadratic bezier: B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2"""
    return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2


async def smooth_mouse_move(page: Page, target_x: int, target_y: int) -> None:
    """
    Move mouse smoothly to target position using bezier curves and easing.

    Uses:
    - Quadratic bezier curve for natural curved paths (humans don't move in straight lines)
    - Ease-in-out timing for natural acceleration/deceleration
    - Small tremor noise to simulate hand movement
    """
    # Get viewport dimensions for bounds checking
    viewport = page.viewport_size
    if not viewport:
        viewport = {"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT}

    # Clamp target to viewport
    target_x = max(10, min(target_x, viewport["width"] - 10))
    target_y = max(10, min(target_y, viewport["height"] - 10))

    # Get current mouse position (start from center if unknown)
    try:
        current_pos = await page.evaluate("() => ({ x: window._mouseX || 500, y: window._mouseY || 400 })")
        start_x = current_pos.get("x", 500)
        start_y = current_pos.get("y", 400)
    except Exception:
        start_x, start_y = 500, 400

    # Calculate bezier control point (creates curved path)
    # Control point is offset from the midpoint to create a natural arc
    midpoint_x = (start_x + target_x) / 2
    midpoint_y = (start_y + target_y) / 2
    control_x = midpoint_x + random.uniform(-config.MOUSE_CURVE_VARIANCE, config.MOUSE_CURVE_VARIANCE)
    control_y = midpoint_y + random.uniform(-config.MOUSE_CURVE_VARIANCE / 2, config.MOUSE_CURVE_VARIANCE / 2)

    # Movement parameters
    steps = config.MOUSE_MOVE_STEPS
    duration = random.uniform(config.MOUSE_MOVE_DURATION_MIN, config.MOUSE_MOVE_DURATION_MAX)
    step_delay = duration / steps
    tremor = config.MOUSE_TREMOR_AMOUNT

    # Move along bezier curve with eased timing
    for i in range(1, steps + 1):
        # Linear progress through steps
        t = i / steps
        # Apply ease-in-out for natural acceleration/deceleration
        eased_t = _ease_in_out(t)

        # Position along curved bezier path
        x = _quadratic_bezier(eased_t, start_x, control_x, target_x)
        y = _quadratic_bezier(eased_t, start_y, control_y, target_y)

        # Add small tremor noise (mimics hand movement)
        x += random.uniform(-tremor, tremor)
        y += random.uniform(-tremor, tremor)

        # Clamp to viewport
        x = max(10, min(x, viewport["width"] - 10))
        y = max(10, min(y, viewport["height"] - 10))

        await page.mouse.move(x, y)
        await asyncio.sleep(step_delay)

    # Final move to exact target (no tremor)
    await page.mouse.move(target_x, target_y)

    # Track position for next movement
    await page.evaluate(f"() => {{ window._mouseX = {target_x}; window._mouseY = {target_y}; }}")


async def smooth_scroll(page: Page, amount: int) -> None:
    """
    Perform a smooth scroll by the given amount (positive = down, negative = up).

    Uses actual wheel events via Playwright (generates real wheel events that bot
    detection expects from human scrolling). This is more realistic than CSS
    smooth scrolling which only fires scroll events, not wheel events.
    """
    if amount == 0:
        return

    # Break into multiple small wheel movements (like a mouse wheel)
    num_ticks = random.randint(config.SCROLL_TICKS_MIN, config.SCROLL_TICKS_MAX)

    # Distribute the total scroll amount across ticks with eased weighting
    remaining = amount
    for i in range(num_ticks):
        # Ease-in-out the delta amounts (start/end slow, middle fast)
        t = (i + 0.5) / num_ticks  # Use midpoint of each segment
        weight = _ease_in_out(t)

        # Calculate this tick's delta with some variation
        if i == num_ticks - 1:
            # Last tick gets whatever remains
            tick_delta = remaining
        else:
            # Variable amounts based on easing (more scroll in middle)
            base_delta = amount / num_ticks
            tick_delta = int(base_delta * (0.5 + weight) + random.uniform(-10, 10))
            # Don't overshoot
            if abs(tick_delta) > abs(remaining):
                tick_delta = remaining
            remaining -= tick_delta

        # Fire actual wheel event
        await page.mouse.wheel(0, tick_delta)

        # Variable delay between ticks (human timing)
        if i < num_ticks - 1:  # No delay after last tick
            await asyncio.sleep(random.uniform(
                config.SCROLL_TICK_DELAY_MIN,
                config.SCROLL_TICK_DELAY_MAX
            ))


async def simulate_human_behavior(page: Page, duration: float) -> None:
    """
    Simulate human behavior for the given duration.
    Probabilistically performs actions - not every action every time:
    - ~50% chance: smooth mouse movement to random position
    - ~30% chance: random scroll (up or down)
    - ~20% chance: just pause (do nothing)
    """
    viewport = page.viewport_size
    if not viewport:
        viewport = {"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT}

    start = time.time()
    while time.time() - start < duration:
        remaining = duration - (time.time() - start)
        if remaining < 0.5:
            break

        action = random.choices(
            ['mouse', 'scroll', 'pause'],
            weights=[0.5, 0.3, 0.2]
        )[0]

        try:
            if action == 'mouse':
                # Move to random position within viewport
                target_x = random.randint(100, viewport["width"] - 100)
                target_y = random.randint(100, viewport["height"] - 100)
                await smooth_mouse_move(page, target_x, target_y)
            elif action == 'scroll':
                # Random scroll amount and direction
                amount = random.randint(config.SCROLL_AMOUNT_MIN, config.SCROLL_AMOUNT_MAX)
                if random.random() < 0.3:  # 30% chance to scroll up
                    amount = -amount
                await smooth_scroll(page, amount)
        except Exception as e:
            logger.debug(f"Human behavior action '{action}' failed: {e}")
            # Continue with next action rather than failing

        # Pause between actions
        await asyncio.sleep(random.uniform(0.5, 1.5))


async def inject_mouse_tracker(page: Page) -> None:
    """
    Inject a visual indicator that follows the mouse cursor.
    Useful for debugging mouse movements in headed mode.
    """
    await page.evaluate('''
        (() => {
            if (document.getElementById('debug-cursor')) return;
            const cursor = document.createElement('div');
            cursor.id = 'debug-cursor';
            cursor.style.cssText = `
                position: fixed;
                width: 20px;
                height: 20px;
                border-radius: 50%;
                background: rgba(255, 0, 0, 0.5);
                border: 2px solid red;
                pointer-events: none;
                z-index: 999999;
                transform: translate(-50%, -50%);
            `;
            document.body.appendChild(cursor);
            document.addEventListener('mousemove', e => {
                cursor.style.left = e.clientX + 'px';
                cursor.style.top = e.clientY + 'px';
            });
        })()
    ''')


async def human_like_interaction(page: Page) -> None:
    """
    Perform subtle human-like interactions before main actions.
    Legacy function - now just performs a quick mouse movement.
    Use simulate_human_behavior() for more comprehensive simulation.
    """
    viewport = page.viewport_size
    if not viewport:
        viewport = {"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT}

    x = random.randint(100, viewport["width"] - 100)
    y = random.randint(100, viewport["height"] - 100)
    await smooth_mouse_move(page, x, y)
