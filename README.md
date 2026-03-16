# Fruit & Veg Price Scraper

This tool automatically checks competitor store websites to find prices for fruit and veg products. It compares their prices to yours and creates a spreadsheet showing who's cheapest for each item.

## One-Time Setup

1. **Install Python** (version 3.10 or newer)
   - Download from https://www.python.org/downloads/
   - During install, tick "Add Python to PATH"

2. **Open a terminal** in this folder
   - Windows: Right-click the folder > "Open in Terminal"
   - Or: Open Command Prompt, type `cd path\to\price-scraper`

3. **Install requirements** (run these commands once):
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

4. **Install Claude CLI** (required — powers the AI evaluation pipeline):
   ```
   npm install -g @anthropic-ai/claude-code
   ```
   Note: Requires Node.js - download from https://nodejs.org/ if needed

5. **Configure store targets**:
   - Copy `scraper/stores/store_config.example.py` to `scraper/stores/store_config.py`
   - Edit with your target store URLs, CSS selectors, and locale settings

6. **Configure business thresholds**:
   - Copy `scraper/thresholds.example.py` to `scraper/thresholds.py`
   - Adjust pricing and SWOT analysis thresholds for your use case

7. **Set up database connection** (only for full pipeline):
   - Copy `.env.example` to `.env` and edit with your database credentials
   - Copy `scraper/db_schema.example.py` to `scraper/db_schema.py` and customise for your database

## How to Run

### Recommended: Interactive Wizard

The easiest way to use the tool. Requires database connection (`.env` file).

```
python -m scraper
```

The wizard walks you through selecting an offer, configuring options,
scraping prices, reviewing results in an approval screen, and optionally
updating the database.

### Option B: Scrape from CSV file

If you have a CSV file with items to check:

```
python -m scraper.main --input input/items.csv --output output
```

A browser window will open and visit the configured store websites. Wait for it to finish (you'll see "SCRAPE COMPLETE").

### Option C: Full automated pipeline

If you want to pull items from the database, scrape, and review updates:

```
python -m scraper --offer-id 123 --dry-run
```

Replace `123` with your offer ID. The `--dry-run` flag shows what would change without actually updating the database.

### Headless mode (no visible browser)

Add `--headless` to run in the background:

```
python -m scraper.main --input input/items.csv --output output --headless
```

## What You Get

After running, check the `output` folder:

| File | What it contains |
|------|------------------|
| `results_YYYY-MM-DD_HHMMSS.json` | Raw data from the scrape |
| `comparison.csv` | Spreadsheet comparing all prices |
| `report_*.html` | HTML summary report with SWOT analysis |
| `pipeline_audit_*.log` | Record of any database updates |

When using the wizard or pipeline, you'll also see an **approval screen** where you can review each price comparison, select which items to update, and confirm before any database changes are made. Use `--dry-run` to preview without modifying the database.

The `comparison.csv` file has columns showing:
- Our price vs Store A price vs Store B price
- Which is cheapest
- Match quality (good/ok/poor) — AI-rated confidence that the competitor product is the same item

## How It Works

The pipeline uses Claude LLMs at three stages to automate tasks that would otherwise require manual judgment:

```
DB/CSV
  → Search Term Generation (Claude Haiku)
    → Browser Scraping (Playwright)
      → Product Matching & Evaluation (Claude Sonnet, 8 parallel)
        → Price Comparison
          → Approval TUI
            → DB Update & Report + SWOT Analysis (Claude Sonnet)
```

### 1. Search Term Generation (Claude Haiku)

Internal product names like "Apples - Fuji - approx. 157g" aren't useful as search queries. Haiku transforms them into natural search terms ("Fuji Apples") and extracts weight/quantity metadata for later price conversion. Items are batched (200 per LLM call) for efficiency.

### 2. Product Matching & Evaluation (Claude Sonnet)

After the browser scrapes the top 3 search results per item from each store, Sonnet evaluates each result set. For every item it:

- Picks the best match from up to 3 candidates per store
- Rates match quality (good / ok / poor / none)
- Extracts pack sizes and quantity multipliers so a 4-pack can be fairly compared to a single unit

Each item gets its own Claude CLI subprocess call, with 8 running in parallel via `ThreadPoolExecutor`. Mathematical guardrails then validate the LLM-suggested multipliers against known weights and quantities before any price conversion is applied.

### 3. SWOT Analysis (Claude Sonnet)

Once the price comparison is complete, Sonnet synthesises the full dataset — pricing position, store coverage, match quality, RRP movement — into a strategic SWOT analysis for the HTML report. If the LLM call fails, a rule-based fallback generates the SWOT from configurable thresholds (see Settings).

## Common Issues

### "playwright install chromium" fails
- Make sure you have internet access
- Try running as administrator

### Browser gets blocked / no results
- Wait a few minutes and try again
- Try running only one store: `--store store_a` or `--store store_b`
- Try without headless mode (visible browser works better)
- Try using a proxy: `--proxy http://your-proxy:8080`

### "ModuleNotFoundError: No module named 'scraper'"
- Make sure you're in the project root folder
- Run: `pip install -r requirements.txt`

### "Store config not found" or "Database schema not found"
- Copy the example config files as described in Setup steps 5-6
- Fill in your actual values

### "Claude CLI not found"
- This only affects the processor step
- Install with: `npm install -g @anthropic-ai/claude-code`

## Quick Reference

| Command | What it does |
|---------|--------------|
| `python -m scraper` | Interactive wizard (recommended) |
| `python -m scraper.main --input items.csv --output output` | Scrape prices from CSV |
| `python -m scraper.main --input items.csv --output output --limit 5` | Test with 5 items |
| `python -m scraper --offer-id 123 --dry-run` | Preview pipeline changes |
| `python -m scraper --offer-id 123` | Run pipeline and update DB |
| `python -m scraper --offer-id 123 --fully-automated` | Auto-accept good/ok matches |
| `python -m scraper --offer-id 123 --skip-scrape --results output/results.json` | Re-process existing results |
