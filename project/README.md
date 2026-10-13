# Germany Product Designer Job Monitor

Production-grade monitor for Product Design jobs in Germany. Scrapes Indeed
and LinkedIn on a schedule, filters and deduplicates, and sends one Telegram
notification per newly discovered job.

Full architecture and specifications live in the Obsidian vault
(`JobMonitor-Obsidian-Vault/Projects/Germany Job Monitor/`) — the single
source of truth for this project.

## Requirements

- Python 3.13+ (managed by uv)
- [uv](https://docs.astral.sh/uv/)
- A Telegram bot token and chat ID

## Installation

```
uv sync
uv run playwright install chromium
```

## Configuration

```
copy .env.example .env      # Windows (cp on Linux/macOS)
```

Fill the two required values in `.env`:

- `TELEGRAM_TOKEN` — from [@BotFather](https://t.me/BotFather) (`/newbot`)
- `CHAT_ID` — send your bot a message, then read the chat id from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`

Everything else has safe defaults (see `.env.example`). Verify with:

```
uv run python app.py check
```

## Running

```
uv run python app.py run            # continuous monitoring (Ctrl+C to stop)
uv run python app.py run --once     # one monitoring cycle, then exit
uv run python app.py health         # startup health report
uv run python app.py stats          # database statistics + recent jobs
```

Startup runs health checks first. A critical failure (no internet, broken
database, missing dependencies) blocks the start. A Playwright failure only
disables the LinkedIn scraper — Indeed keeps running.

## Testing

```
uv run pytest                       # full suite
uv run pytest -m "not browser"      # skip the real-Chromium tests
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Startup blocked by health checks: internet: critical` | No network connectivity; check proxy/firewall |
| `telegram: warning (getMe failed)` | Wrong `TELEGRAM_TOKEN`; the app still scrapes, notifications are disabled |
| `LinkedIn scraper disabled` | Chromium missing — run `uv run playwright install chromium` |
| `LinkedIn access blocked (login wall)` in logs | Expected with anonymous scraping; the run stops safely and retries next cycle |
| `Indeed blocked the request (HTTP 403)` | Bot detection; the Playwright fallback is attempted, then the keyword is skipped for this cycle |
| Selector warnings (`layout change?`) | Site DOM changed — update the selector registry in `scrapers/*.py` and the vault's Selectors.md |
| Database locked errors | Another process holds `database/jobs.db`; close it (inserts retry automatically) |

Logs: console + `logs/app.log` (10 MB rotation, 10 files kept). Secrets are
never logged.

## Project layout

Flat modules per the vault's Project Structure.md: one file per framework
component (`browser.py`, `http_client.py`, `pipeline.py`, …), one scraper
per site under `scrapers/`, domain models under `models/`, Alembic
migrations under `database/migrations/`, tests under `tests/`.

Adding a website = create a scraper inheriting `BaseScraper`, register it in
`app.create_services` — nothing else changes.
