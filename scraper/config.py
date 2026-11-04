# Timing - expanded ranges for better anti-detection (50-100% more variance)
DELAY_BETWEEN_SEARCHES_MIN: float = 3.0   # was 4.0
DELAY_BETWEEN_SEARCHES_MAX: float = 12.0  # was 8.0

# The user agent is derived from the running Chromium build in utils/stealth.py.

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
