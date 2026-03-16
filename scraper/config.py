# Timing - expanded ranges for better anti-detection (50-100% more variance)
DELAY_BETWEEN_SEARCHES_MIN: float = 3.0   # was 4.0
DELAY_BETWEEN_SEARCHES_MAX: float = 12.0  # was 8.0

# User agent pool - rotated per session for fingerprint variance
# Real user agents from recent Chrome/Firefox on Windows/Mac
USER_AGENTS: list[str] = [
    # Chrome on Windows 10/11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# Legacy single user agent (for backwards compatibility, prefer USER_AGENTS pool)
USER_AGENT: str = USER_AGENTS[0]

SELECTOR_TIMEOUT: int = 10000  # ms to wait for product tiles

# Human behavior simulation settings - expanded ranges for realism
MOUSE_MOVE_STEPS: int = 25                # steps for smooth movement
MOUSE_MOVE_DURATION_MIN: float = 0.2      # was 0.3 - min seconds per movement
MOUSE_MOVE_DURATION_MAX: float = 1.5      # was 0.8 - max seconds per movement
SCROLL_AMOUNT_MIN: int = 100              # min pixels per scroll
SCROLL_AMOUNT_MAX: int = 400              # max pixels per scroll

# Bezier curve settings for natural mouse movement
MOUSE_CURVE_VARIANCE: int = 50            # max pixels control point can deviate from midpoint
MOUSE_TREMOR_AMOUNT: float = 1.5          # max pixels of noise per step

# Wheel scroll settings for realistic scrolling
SCROLL_TICKS_MIN: int = 3                 # min wheel events per scroll
SCROLL_TICKS_MAX: int = 8                 # max wheel events per scroll
SCROLL_TICK_DELAY_MIN: float = 0.03       # min delay between wheel events (seconds)
SCROLL_TICK_DELAY_MAX: float = 0.12       # max delay between wheel events (seconds)

MAX_RESULTS_PER_STORE: int = 3
MAX_TILES_TO_CHECK: int = 9  # Check more tiles to find ones with prices (some OOS items lack prices)

RETRY_COUNT: int = 1

PAGE_LOAD_TIMEOUT: int = 30000

VIEWPORT_WIDTH: int = 1920
VIEWPORT_HEIGHT: int = 1080
