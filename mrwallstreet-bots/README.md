# Mr Wall Street — Discord Bots

Automated market content for the **Mr Wall Street** Discord server.
Runs for free on GitHub Actions — no server, no hosting costs.

## Channels powered

| Channel | Script | Schedule | Source |
|---|---|---|---|
| `#market-news` | `scripts/market_news.py` | every 2h, Mon–Fri | CNBC + MarketWatch RSS (no key) |
| `#stock-earnings` | `scripts/earnings.py preview` | Sunday 16:05 Dubai | Finnhub (free key) |
| `#stock-earnings` | `scripts/earnings.py results` | hourly in reporting windows, Mon–Fri | Finnhub (free key) |
| `#economic-data` | `scripts/economic_data.py` | Sunday 16:00 Dubai | ForexFactory free feed (no key) |
| `#premarket-movers` | *(coming next)* | daily, pre-open | Yahoo Finance |

## Setup (one time)

1. Create a **private GitHub repo** and push these files.
2. In Discord, create a webhook for each channel
   (channel → gear icon → Integrations → Webhooks → New Webhook → Copy URL).
3. Create a free account at finnhub.io and copy your API key.
4. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `DISCORD_WEBHOOK_MARKET_NEWS` — webhook URL of `#market-news`
   - `DISCORD_WEBHOOK_ECONOMIC_DATA` — webhook URL of `#economic-data`
   - `DISCORD_WEBHOOK_STOCK_EARNINGS` — webhook URL of `#stock-earnings`
   - `FINNHUB_API_KEY` — your Finnhub key
5. Go to the **Actions** tab → pick a workflow → **Run workflow** to test each one.
6. Done — everything now runs on schedule automatically.

## How the earnings channel works

- **Sunday afternoon**: posts the week-ahead calendar (who reports, which day,
  before/after market, EPS estimates).
- **During the week**: checks Finnhub hourly around pre-market and after-hours
  windows; the moment a watchlist company's actual results appear, it posts
  EPS + revenue vs estimates with a BEAT/MISS verdict. Never posts the same
  result twice.
- Only companies in the `WATCHLIST` inside `scripts/earnings.py` are posted
  (~120 large caps + high-interest names). Edit that list to taste.

## Notes

- Bots remember what they already posted (`state/` folder) so they never repeat.
- Max 5 headlines per news run — keeps channels clean.
- All sources are free; total cost of running this repo is $0.
