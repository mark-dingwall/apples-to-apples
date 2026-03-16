"""SWOT analysis builders — rule-based and LLM-based."""

import json
import logging
import os
import re

from scraper.stores.store_config import STORE_A_NAME, STORE_B_NAME
from scraper.utils.claude_cli import call_claude_cli
from scraper.wizard.settings import SettingsManager

try:
    from scraper.prompts import SWOT_LLM_PROMPT
except ImportError:
    raise ImportError(
        "Prompt templates not found. Copy scraper/prompts.example.py "
        "to scraper/prompts.py and customise for your use case."
    )

logger = logging.getLogger(__name__)


def build_swot(
    positioning: dict, coverage: dict, cheapest: dict, rrp_mov: dict,
) -> dict[str, list[str]]:
    """Build SWOT quadrant content using rules (no LLM)."""
    settings = SettingsManager().load()
    strengths = []
    weaknesses = []
    opportunities = []
    threats = []

    total = positioning["total"] or 1

    # --- Strengths ---
    below_pct = positioning["below"] / total * 100
    if below_pct >= settings.swot_below_rrp_strength_pct:
        strengths.append(f"{below_pct:.0f}% of items priced below RRP")

    if coverage["total"] > 0:
        total_matches = coverage["store_a_matches"] + coverage["store_b_matches"]
        total_good = coverage["store_a_good"] + coverage["store_b_good"]
        if total_matches > 0 and total_good / total_matches * 100 >= settings.swot_good_quality_strength_pct:
            strengths.append(
                f"High match quality ({total_good}/{total_matches} good)"
            )

        both_pct = coverage["both"] / coverage["total"] * 100
        if both_pct >= settings.swot_dual_coverage_strength_pct:
            strengths.append(f"Dual-store coverage on {both_pct:.0f}% of items")

    if cheapest["total"] > 0:
        us_pct = cheapest["us"] / cheapest["total"] * 100
        if us_pct >= settings.swot_cheapest_strength_pct:
            strengths.append(f"Cheapest on {us_pct:.0f}% of comparable items")

    # --- Weaknesses ---
    above_pct = positioning["above"] / total * 100
    if above_pct >= settings.swot_above_rrp_weakness_pct:
        weaknesses.append(f"{positioning['above']} items priced above RRP")

    if coverage["total"] > 0:
        total_matches = coverage["store_a_matches"] + coverage["store_b_matches"]
        total_poor = coverage["store_a_poor"] + coverage["store_b_poor"]
        if total_matches > 0 and total_poor / total_matches * 100 >= settings.swot_poor_quality_weakness_pct:
            weaknesses.append(f"{total_poor} poor quality matches")

        if coverage["neither"] > 0:
            weaknesses.append(
                f"{coverage['neither']} items with no store matches"
            )

    # --- Opportunities ---
    if rrp_mov["total"] > 0:
        if rrp_mov["increased"] > 0:
            opportunities.append(
                f"{rrp_mov['increased']} RRP increases -- room to raise prices"
            )

    if positioning["below"] > 0 and positioning["avg_margin"] < -settings.swot_margin_uplift_pct:
        opportunities.append(
            f"Avg {abs(positioning['avg_margin']):.1f}% below RRP -- margin uplift potential"
        )

    # --- Threats ---
    if rrp_mov["total"] > 0 and rrp_mov["decreased"] > 0:
        threats.append(
            f"{rrp_mov['decreased']} RRP decreases -- competitors dropping prices"
        )

    if cheapest["total"] > 0:
        us_pct = cheapest["us"] / cheapest["total"] * 100
        if us_pct < settings.swot_cheapest_threat_pct:
            threats.append(f"Only cheapest on {us_pct:.0f}% of items")

    return {
        "strengths": strengths, "weaknesses": weaknesses,
        "opportunities": opportunities, "threats": threats,
    }


def build_swot_llm(
    positioning: dict, coverage: dict, cheapest: dict, rrp_mov: dict,
    concerns: list[str], outliers: dict,
) -> dict[str, list[str]] | None:
    """Build SWOT using LLM. Returns None on failure (caller should fall back)."""
    total = positioning["total"] or 1
    below_pct = positioning["below"] / total * 100
    above_pct = positioning["above"] / total * 100

    cheapest_total = cheapest["total"] or 1
    us_cheapest_pct = cheapest["us"] / cheapest_total * 100

    total_matches = coverage["store_a_matches"] + coverage["store_b_matches"]
    total_good = coverage["store_a_good"] + coverage["store_b_good"]
    total_poor = coverage["store_a_poor"] + coverage["store_b_poor"]
    good_pct = (total_good / total_matches * 100) if total_matches else 0
    poor_pct = (total_poor / total_matches * 100) if total_matches else 0

    rrp_total = rrp_mov["total"] or 1
    inc_pct = rrp_mov["increased"] / rrp_total * 100
    dec_pct = rrp_mov["decreased"] / rrp_total * 100

    top_above = ", ".join(
        f"{name[:25]} (+{pct:.0f}%)" for name, _, _, _, pct in outliers.get("above_rrp", [])[:3]
    )
    top_below = ", ".join(
        f"{name[:25]} (-{pct:.0f}%)" for name, _, _, _, pct in outliers.get("below_rrp", [])[:3]
    )

    data_summary = (
        f"PRICE POSITIONING: {positioning['below']} items below RRP ({below_pct:.1f}%), "
        f"{positioning['above']} above ({above_pct:.1f}%), "
        f"{positioning['equal']} equal. "
        f"Avg margin vs RRP: {positioning['avg_margin']:+.1f}%, "
        f"median: {positioning['med_margin']:+.1f}%.\n"
        f"STORE COVERAGE: {coverage['total']} items total. "
        f"{STORE_A_NAME} matched {coverage['store_a_matches']}, {STORE_B_NAME} matched {coverage['store_b_matches']}. "
        f"Both stores: {coverage['both']}, neither: {coverage['neither']}. "
        f"Match quality: {good_pct:.0f}% good, {poor_pct:.0f}% poor.\n"
        f"CHEAPEST: Us {cheapest['us']} ({us_cheapest_pct:.0f}%), "
        f"{STORE_A_NAME} {cheapest['store_a']}, {STORE_B_NAME} {cheapest['store_b']} "
        f"(of {cheapest['total']} comparable).\n"
        f"RRP MOVEMENT: {rrp_mov['increased']} increased ({inc_pct:.0f}%), "
        f"{rrp_mov['decreased']} decreased ({dec_pct:.0f}%), "
        f"{rrp_mov['unchanged']} unchanged. "
        f"Avg increase: ${rrp_mov['avg_inc'] / 100:.2f}, "
        f"avg decrease: ${rrp_mov['avg_dec'] / 100:.2f}.\n"
        f"MOST EXPENSIVE vs RRP: {top_above or 'none'}.\n"
        f"BEST VALUE vs RRP: {top_below or 'none'}.\n"
    )
    if concerns:
        data_summary += "CONCERNS: " + "; ".join(concerns) + ".\n"

    prompt = SWOT_LLM_PROMPT.format(
        store_a_name=STORE_A_NAME,
        store_b_name=STORE_B_NAME,
        data_summary=data_summary,
    )

    model = os.getenv("SWOT_MODEL", "sonnet")
    raw = call_claude_cli(prompt, model=model)
    if not raw:
        logger.warning("SWOT LLM call returned no output, falling back to rules")
        return None

    try:
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if not json_match:
            logger.warning("SWOT LLM response contained no JSON")
            return None

        parsed = json.loads(json_match.group())

        for key in ("strengths", "weaknesses", "opportunities", "threats"):
            if key not in parsed or not isinstance(parsed[key], list):
                logger.warning(f"SWOT LLM response missing or invalid '{key}' key")
                return None
            parsed[key] = [str(item) for item in parsed[key]]

        return parsed

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"SWOT LLM response was not valid JSON: {e}")
        return None
