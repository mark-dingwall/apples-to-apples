"""Settings management for the wizard."""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from scraper.thresholds import (
        SIGNIFICANT_PRICE_DIFF_PCT,
        SWOT_BELOW_RRP_STRENGTH_PCT,
        SWOT_GOOD_QUALITY_STRENGTH_PCT,
        SWOT_DUAL_COVERAGE_STRENGTH_PCT,
        SWOT_CHEAPEST_STRENGTH_PCT,
        SWOT_ABOVE_RRP_WEAKNESS_PCT,
        SWOT_POOR_QUALITY_WEAKNESS_PCT,
        SWOT_MARGIN_UPLIFT_PCT,
        SWOT_CHEAPEST_THREAT_PCT,
    )
except ImportError:
    raise ImportError(
        "Thresholds config not found. Copy scraper/thresholds.example.py "
        "to scraper/thresholds.py and adjust values for your use case."
    )

logger = logging.getLogger(__name__)


@dataclass
class Settings:
    """User-configurable settings."""

    significant_price_diff_pct: float = SIGNIFICANT_PRICE_DIFF_PCT
    default_batch_size: int = 10
    search_term_batch_size: int = 200
    page_size: int = 20
    default_headless: bool = False
    output_dir: str = "output"

    # SWOT thresholds
    swot_below_rrp_strength_pct: float = SWOT_BELOW_RRP_STRENGTH_PCT
    swot_good_quality_strength_pct: float = SWOT_GOOD_QUALITY_STRENGTH_PCT
    swot_dual_coverage_strength_pct: float = SWOT_DUAL_COVERAGE_STRENGTH_PCT
    swot_cheapest_strength_pct: float = SWOT_CHEAPEST_STRENGTH_PCT
    swot_above_rrp_weakness_pct: float = SWOT_ABOVE_RRP_WEAKNESS_PCT
    swot_poor_quality_weakness_pct: float = SWOT_POOR_QUALITY_WEAKNESS_PCT
    swot_margin_uplift_pct: float = SWOT_MARGIN_UPLIFT_PCT
    swot_cheapest_threat_pct: float = SWOT_CHEAPEST_THREAT_PCT

    # Quality & pricing strategy
    quality_ranking: dict = field(default_factory=lambda: {"good": 3, "ok": 2, "poor": 1, "none": 0})
    auto_approve_qualities: list = field(default_factory=lambda: ["good", "ok"])
    guardrail_tolerance: float = 0.01

    # CSV schema
    category_id_fallback: str = ""


class SettingsManager:
    """Manages loading and saving settings to/from JSON file."""

    DEFAULT_PATH = Path("settings.json")

    def __init__(self, path: Path | None = None):
        self.path = path or self.DEFAULT_PATH
        self._settings: Settings | None = None

    def load(self) -> Settings:
        """Load settings from file, creating with defaults if missing."""
        if self._settings is not None:
            return self._settings

        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self._settings = self._from_dict(data)
                logger.debug(f"Loaded settings from {self.path}")
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Failed to load settings from {self.path}: {e}")
                logger.warning("Using default settings")
                self._settings = Settings()
                self.save()  # Save defaults
        else:
            logger.info(f"Settings file not found, creating {self.path}")
            self._settings = Settings()
            self.save()

        return self._settings

    def save(self, settings: Settings | None = None) -> None:
        """Save settings to file."""
        if settings is not None:
            self._settings = settings

        if self._settings is None:
            self._settings = Settings()

        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(asdict(self._settings), f, indent=2)
            logger.debug(f"Saved settings to {self.path}")
        except IOError as e:
            logger.error(f"Failed to save settings: {e}")
            raise

    def _from_dict(self, data: dict[str, Any]) -> Settings:
        """Create Settings from a dictionary, handling missing/extra keys."""
        defaults = Settings()
        return Settings(
            significant_price_diff_pct=data.get(
                "significant_price_diff_pct", defaults.significant_price_diff_pct
            ),
            default_batch_size=data.get(
                "default_batch_size", defaults.default_batch_size
            ),
            search_term_batch_size=data.get(
                "search_term_batch_size", defaults.search_term_batch_size
            ),
            page_size=data.get("page_size", defaults.page_size),
            default_headless=data.get("default_headless", defaults.default_headless),
            output_dir=data.get("output_dir", defaults.output_dir),
            swot_below_rrp_strength_pct=data.get(
                "swot_below_rrp_strength_pct", defaults.swot_below_rrp_strength_pct
            ),
            swot_good_quality_strength_pct=data.get(
                "swot_good_quality_strength_pct", defaults.swot_good_quality_strength_pct
            ),
            swot_dual_coverage_strength_pct=data.get(
                "swot_dual_coverage_strength_pct", defaults.swot_dual_coverage_strength_pct
            ),
            swot_cheapest_strength_pct=data.get(
                "swot_cheapest_strength_pct", defaults.swot_cheapest_strength_pct
            ),
            swot_above_rrp_weakness_pct=data.get(
                "swot_above_rrp_weakness_pct", defaults.swot_above_rrp_weakness_pct
            ),
            swot_poor_quality_weakness_pct=data.get(
                "swot_poor_quality_weakness_pct", defaults.swot_poor_quality_weakness_pct
            ),
            swot_margin_uplift_pct=data.get(
                "swot_margin_uplift_pct", defaults.swot_margin_uplift_pct
            ),
            swot_cheapest_threat_pct=data.get(
                "swot_cheapest_threat_pct", defaults.swot_cheapest_threat_pct
            ),
            quality_ranking=data.get("quality_ranking", defaults.quality_ranking),
            auto_approve_qualities=data.get(
                "auto_approve_qualities", defaults.auto_approve_qualities
            ),
            guardrail_tolerance=data.get(
                "guardrail_tolerance", defaults.guardrail_tolerance
            ),
            category_id_fallback=data.get(
                "category_id_fallback", defaults.category_id_fallback
            ),
        )

    def update(self, **kwargs: Any) -> Settings:
        """Update specific settings and save."""
        settings = self.load()
        for key, value in kwargs.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        self.save(settings)
        return settings
