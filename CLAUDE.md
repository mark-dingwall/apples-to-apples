# CLAUDE.md

Fruit & veg price scraper: compares internal prices against competitor stores.

## Quick Start

```bash
# Install
pip install -r requirements.txt
playwright install chromium
npm install -g @anthropic-ai/claude-code  # Required for LLM processing

# Configure
cp scraper/stores/store_config.example.py scraper/stores/store_config.py  # Fill in store details
cp scraper/db_schema.example.py scraper/db_schema.py                      # Fill in DB schema
cp scraper/prompts.example.py scraper/prompts.py                          # Fill in LLM prompts
cp scraper/thresholds.example.py scraper/thresholds.py                    # Adjust business thresholds
cp .env.example .env                                                      # Fill in DB credentials

# Run (interactive wizard - recommended)
python -m scraper

# Or with CLI flags (automation/scripting)
python -m scraper --offer-id 123 --dry-run
```

## Project Structure

```
scraper/
├── __main__.py             # Entry point (wizard or pipeline router)
├── wizard/                 # TUI wizard module
│   ├── runner.py           # Wizard orchestration
│   ├── settings.py         # Settings manager
│   ├── state.py            # Wizard state
│   ├── components/         # Reusable TUI components
│   │   ├── menu.py         # Selectable menu
│   │   ├── form.py         # Form/checkbox component
│   │   ├── help_box.py     # Help display widget
│   │   └── help_content.py # Help text content
│   └── steps/              # Wizard step implementations
│       ├── offer_select.py
│       ├── cache_check.py
│       ├── options.py
│       ├── progress.py
│       ├── approval.py
│       └── report.py
├── main.py              # Scraper core
├── processor.py         # LLM post-processor
├── pipeline.py          # CLI automation (when flags passed to __main__)
├── config.py            # Settings (delays, viewport, behavior simulation)
├── models.py            # Data classes
├── db.py                # MySQL operations (imports queries from db_schema.py)
├── db_schema.py         # Database queries & column names (GITIGNORED)
├── db_schema.example.py # Template for db_schema.py
├── tui.py               # Rich terminal UI (approval screen)
├── html_report.py       # HTML report generator
├── prompts.py           # LLM prompt templates (GITIGNORED)
├── prompts.example.py   # Template for prompts.py
├── thresholds.py        # Business thresholds (GITIGNORED)
├── thresholds.example.py # Template for thresholds.py
├── oos_mode.py          # OOS detection learning
├── inspect_mode.py      # Debugging helper
├── stores/
│   ├── base.py                 # Abstract scraper
│   ├── store_a.py              # Store A implementation
│   ├── store_b.py              # Store B implementation
│   ├── store_config.py         # Store URLs, selectors, settings (GITIGNORED)
│   └── store_config.example.py # Template for store_config.py
└── utils/
    ├── claude_cli.py    # Claude CLI wrapper
    ├── stealth.py       # Anti-detection (Bezier mouse)
    ├── matching.py      # Fuzzy matching, term extraction
    ├── oos_detection.py # OOS heuristics
    └── swot.py          # SWOT analysis builders
```

**Project root:**
- `regen_report.py` — Developer utility: regenerate HTML report from existing DB data (`python regen_report.py <offer_id>`)

## Configuration

### Gitignored config files

These files contain sensitive/proprietary configuration and are not committed to git:

| File | Template | Purpose |
|------|----------|---------|
| `scraper/stores/store_config.py` | `store_config.example.py` | Store URLs, CSS selectors, locale |
| `scraper/db_schema.py` | `db_schema.example.py` | SQL queries, column names, category IDs |
| `scraper/prompts.py` | `prompts.example.py` | LLM prompt templates |
| `scraper/thresholds.py` | `thresholds.example.py` | Business thresholds for pricing and SWOT |
| `.env` | `.env.example` | Database credentials |

Copy each template and fill in your values. The app will show a helpful error if any are missing.

## Settings

User-configurable settings are stored in `settings.json` (auto-created on first run):

| Setting | Default | Description |
|---------|---------|-------------|
| `significant_price_diff_pct` | from `thresholds.py` | Highlight items with price difference > this % |
| `default_batch_size` | 10 | (Legacy) Not used - processor now uses parallel subagents |
| `search_term_batch_size` | 200 | Items per search term generation batch |
| `page_size` | 20 | Items per page in approval TUI |
| `default_headless` | false | Run browser headless by default |
| `output_dir` | "output" | Output directory |
| `swot_below_rrp_strength_pct` | from `thresholds.py` | SWOT: % of items below RRP to trigger "strength" |
| `swot_good_quality_strength_pct` | from `thresholds.py` | SWOT: % of good-quality matches to trigger "strength" |
| `swot_dual_coverage_strength_pct` | from `thresholds.py` | SWOT: % dual-store coverage to trigger "strength" |
| `swot_cheapest_strength_pct` | from `thresholds.py` | SWOT: % cheapest items to trigger "strength" |
| `swot_above_rrp_weakness_pct` | from `thresholds.py` | SWOT: % of items above RRP to trigger "weakness" |
| `swot_poor_quality_weakness_pct` | from `thresholds.py` | SWOT: % of poor-quality matches to trigger "weakness" |
| `swot_margin_uplift_pct` | from `thresholds.py` | SWOT: average RRP uplift % to trigger "opportunity" |
| `swot_cheapest_threat_pct` | from `thresholds.py` | SWOT: % competitor-cheapest items to trigger "threat" |
| `quality_ranking` | `{"good":3,"ok":2,"poor":1,"none":0}` | Numeric rank for each quality tier (used in RRP selection) |
| `auto_approve_qualities` | `["good","ok"]` | Quality tiers auto-accepted in `--fully-automated` mode |
| `guardrail_tolerance` | 0.01 | Float tolerance for RRP guardrail comparisons |
| `category_id_fallback` | `""` | Fallback CSV column name for category_id (if primary column absent) |

Edit via the wizard's Options screen (press E) or directly in the JSON file.

## TUI Wizard Keyboard Shortcuts

### Navigation (all screens)
| Key | Action |
|-----|--------|
| Up/Down | Navigate items |
| Enter | Select/Confirm |
| Q | Quit/Back |

### Options Screen
| Key | Action |
|-----|--------|
| Space | Toggle checkbox |
| E | Edit settings.json |

### Approval Screen
| Key | Action |
|-----|--------|
| Space | Toggle selection (auto-advances to next row) |
| G | Select only "good" quality matches |
| O | Select "good" and "ok" matches |
| A | Select all |
| N | Deselect all |
| H | Toggle help mode (column-by-column explanations) |
| Left/Right | Scroll conversion description / navigate help columns |
| Enter | Confirm updates |
| Q | Quit without changes |

## CLI Options (Advanced)

For automation or scripting, pass flags directly:

### python -m scraper (with flags)
| Flag | Description |
|------|-------------|
| `--offer-id` | Offer ID to process (required) |
| `--dry-run` | Show changes without updating DB |
| `--fully-automated` | Skip TUI, auto-accept good/ok matches |
| `--skip-scrape` | Re-process existing results |
| `--results` | Path to existing results JSON |
| `--limit N` | Process first N items |
| `--headless` | Run browser headless |
| `--output-dir` | Output directory (default: output) |

### scraper.main (standalone scraper)
| Flag | Description |
|------|-------------|
| `--input` | Input CSV path (required) |
| `--output` | Output directory (required) |
| `--headless` | Run browser headless |
| `--store` | `store_a`, `store_b`, or `all` (default: all) |
| `--limit N` | Limit to first N items (testing) |
| `--run-id` | Custom identifier for output filename (default: timestamp) |
| `--debug-mouse` | Overlay red cursor to visualize Bezier mouse movements (debugging) |
| `--interactive-oos-check` | OOS learning mode (single store only) |
| `--proxy` | Proxy server URL (e.g., `http://proxy:8080`) |
| `--progress-file` | Path to write progress updates (JSON with completed/total) |

### scraper.processor
| Flag | Description |
|------|-------------|
| `--input` | Input CSV path (required) |
| `--results` | Scraper JSON results (required) |
| `--output` | Output CSV path (required) |
| `--progress-file` | Path to write progress updates (JSON) |
| `--max-workers` | Number of parallel workers (default: 8) |

## Developer Tools

Internal debugging tools (not part of standard workflow):

### scraper.inspect_mode
Opens browser for CSS selector debugging.
```bash
python -m scraper.inspect_mode --store store_b --search "apples"
```

| Flag | Description |
|------|-------------|
| `--store` | `store_a` or `store_b` (required) |
| `--search` | Search term (default: "apples") |
| `--output` | Output directory for HTML (default: output) |

### scraper.oos_mode
Interactive out-of-stock detection for training OOS heuristics.
```bash
python -m scraper.oos_mode --store store_a --input input/items.csv
```

| Flag | Description |
|------|-------------|
| `--store` | `store_a` or `store_b` (required) |
| `--input` | Input CSV file (required) |
| `--output` | Output directory for HTML samples (default: .ref) |
| `--limit` | Limit to first N items |
| `--baseline` | Known in-stock product for baseline (default: "Pink Lady apples") |

## Input Formats

**For main.py (standalone scraper):**
```csv
id,category_id,name,price
1001,2,Apples - Fuji - approx. 157g,88
```

**For processor.py and pipeline:**
```csv
id,category_id,db_name,search_term,our_price,our_weight_g
1001,2,Apples - Fuji - approx. 157g,Fuji Apples,88,157
```

- `category_id`: category IDs defined in `db_schema.py`
- `price`/`our_price`: cents
- `our_weight_g`: weight in grams (optional, from LLM extraction)

## Output Format

**Scraper output** (`output/results_*.json`):
```json
{
  "run_timestamp": "2024-01-15T10:30:00",
  "items": [...],
  "summary": {"total_items": 50, "store_a_success": 48, "store_b_success": 47}
}
```

**Processor output** (`output/comparison.csv`): Side-by-side price comparison with match quality, converted prices, and `cheapest` column.

## Philosophy

- **Capture liberally**: Scrape top 3 results, let LLM pick best match
- **Anti-detection**: Human-like Bezier curves, random delays, stealth mode
- **Unit conversion**: LLM extracts weights, converts per-kg to our unit pricing
- **RRP calculation**: Configurable pricing strategy (see settings.json)

## Environment

Requires `.env` for database access (copy from `.env.example`):
```
DB_HOST=localhost
DB_PORT=3306
DB_NAME=your_database
DB_USER=xxx
DB_PASSWORD=xxx
```

### SSH Tunnel (Optional)

For secure database access through a bastion host, enable SSH tunneling:

```
SSH_ENABLED=true
SSH_HOST=bastion.example.com
SSH_PORT=22
SSH_USER=deploy
SSH_KEY_PATH=~/.ssh/deploy_key
# SSH_KEY_PASSPHRASE=optional_passphrase
# SSH_REMOTE_BIND_HOST=internal-db.vpc  # defaults to DB_HOST
# SSH_REMOTE_BIND_PORT=3306             # defaults to DB_PORT
```

| Variable | Description | Default |
|----------|-------------|---------|
| `SSH_ENABLED` | Enable SSH tunneling | `false` |
| `SSH_HOST` | SSH server hostname | - |
| `SSH_PORT` | SSH server port | `22` |
| `SSH_USER` | SSH username | - |
| `SSH_KEY_PATH` | Path to private key | `~/.ssh/id_rsa` |
| `SSH_KEY_PASSPHRASE` | Key passphrase (if encrypted) | - |
| `SSH_REMOTE_BIND_HOST` | DB host from SSH perspective | `DB_HOST` |
| `SSH_REMOTE_BIND_PORT` | DB port from SSH perspective | `DB_PORT` |

**Note:** The SSH tunnel uses `paramiko.AutoAddPolicy()` by default, which auto-accepts unknown host keys. For untrusted networks, consider switching to `RejectPolicy` with pre-loaded `known_hosts` in `scraper/db.py`.

## Technical Notes

- **LLM Models**: Two Claude models via CLI:
  - **Haiku**: Wizard search term generation (`progress.py`)
  - **Sonnet**: Item evaluation (`processor.py`) and CLI pipeline search term generation (`pipeline.py` via `call_claude_cli()` default)
- **Parallel Processing**: Processor uses Python ThreadPoolExecutor with 8 parallel Sonnet calls
- **Audit Logs**: Pipeline writes `pipeline_audit_*.log` with all DB updates

## Known Issues / TODOs

- Stores occasionally block after ~30 items (retry helps)
- Some items need manual weight estimation
- OOS detection is heuristic-based, may miss edge cases
- **Specials detection disabled**: The "is on special" scraping from store CSS selectors is unreliable and has been removed from CSV output and report analysis. The scraper still collects the data internally. To re-enable, specials detection will likely need manual scraping investigation and trial-and-error with current site markup.
- **Match quality scoring**: Currently uses categorical good/ok/poor ratings from the LLM. Plan to replace with a numeric 1-5 confidence score for more granular quality-aware RRP selection and filtering.
