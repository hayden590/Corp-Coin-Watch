# corp-coin-watch

Finds new meme-coin contract addresses (CAs), checks whether they're safe and
legit, and alerts you on Discord and/or Telegram.

> **This bot never buys or sells anything.** It has no wallet keys, no signing
> code, and no trading path, and a test fails the build if trading code ever
> shows up. Alerts and paper trading only. Nothing it says is financial advice.

The full design is in [SPEC.md](SPEC.md). It's being built in 7 phases.

## Status

| Phase | What | State |
|---|---|---|
| 1 | Setup, DB, CA extraction, DexScreener source, safety checks, alerts, dry-run | ✅ done |
| 2 | Telegram source, legitimacy checks, full scoring/verdicts | not started |
| 3 | X source (twscrape), auto-discovery, endorsements | not started |
| 4 | Smart wallets, Fomo source | not started |
| 5 | Connections analysis | not started |
| 6 | Charts, exit warnings, text analysis | not started |
| 7 | Outcome tracking, backtesting, paper trading, `qualify` | not started |

## Setup

Requires Python 3.11+.

```bash
git clone <this repo> && cd Corp-Coin-Watch
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then fill in what you have
```

Try it with no keys and no network:

```bash
python main.py --dry-run
```

That runs the whole pipeline on the fake tokens in `sample_data/` (a good coin,
a rug, a honeypot, a pump.fun coin whose safety data is missing, a wallet
address that isn't a token, and so on) and prints the alerts it would send.

### Alert channels (`.env`)

- **Discord:** Server Settings → Integrations → Webhooks → New Webhook → copy
  the URL into `DISCORD_WEBHOOK_URL`.
- **Telegram:** message [@BotFather](https://t.me/BotFather) and send `/newbot`,
  then put the token in `TELEGRAM_BOT_TOKEN`. Send your bot one message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into
  `TELEGRAM_CHAT_ID`.

If neither channel is set, alerts go to the console.

Never commit `.env`. Secrets are kept out of the logs: the log output is
scrubbed of any secret value, and httpx URL logging is switched off because the
Telegram bot token is part of the URL.

## Commands

```bash
python main.py                    # run the live watcher (polls DexScreener)
python main.py --dry-run          # full pipeline on sample data
python main.py check "<text>"     # check any CA / tweet text / link right now (console only)
python main.py check "<text>" --send   # ...and send the alert
python main.py health             # source status, requests this hour, last success
python -m pytest                  # run the tests
```

## How phase 1 works

1. **Sources.** The DexScreener token-profile feed is polled about every 90s,
   with some random jitter. The `check` command takes pasted text as a manual source.
2. **Extraction** (`extract.py`) finds these formats:
   - EVM `0x` + 40 hex
   - Solana base58 strings of 32–44 chars that decode to 32 bytes
   - links from dexscreener, pump.fun, birdeye, etherscan, basescan, bscscan and solscan

   It skips tx hashes and signatures, quote tokens and system programs
   (WETH, USDC, wSOL…), and it strips zero-width characters that people use to
   hide addresses. Every sighting is stored with its source and first-seen time.
3. **Resolve.** DexScreener finds the token's deepest pair on your chains. A
   DexScreener link that holds a *pair* address gets mapped back to its token. If
   there's no pair, the CA is logged and nothing is alerted. That also filters out
   wallet addresses.
4. **Safety** (`safety.py`) gives every check pass / warn / fail / unknown:
   - **Market:** liquidity and pair age.
   - **Solana (RugCheck):** mint/freeze authority, LP locked/burned, and the top-10
     holder %. LP/AMM accounts are excluded from the holder %, but the creator's
     own wallet still counts. Also RugCheck's own danger risks and its "rugged" flag.
   - **EVM (GoPlus):** honeypot/can't-sell, buy/sell tax, dangerous owner
     controls, and the top-10 holder %. The LP pair, burn addresses and locked
     wallets are excluded from the holder %.
   - If an API is down or returns nothing, the check is **unknown**. It is never
     treated as a pass.
5. **Verdict** (`scoring.py`):
   - Any failed check → **DANGER — DO NOT BUY**.
   - A critical check that couldn't be answered → **UNCHECKED**.
   - Otherwise → **UNCONFIRMED**.

   **VERIFIED** and the weighted scores arrive in phase 2. Nothing can override
   DANGER.
6. **Alerts** (`alerts.py`) go to Discord (embed) and/or Telegram (HTML). A CA is
   re-alerted only when its verdict changes. DANGER/UNCHECKED coins that only
   showed up in the DexScreener feed are logged, not alerted, because otherwise
   every rug on DexScreener would ping you. Once someone posts one somewhere
   you watch, you get the "DO NOT BUY" alert. If a source fails 5 times in a row
   you get **one** "source down" alert, and a second alert when it recovers.

## Config

Every config file is optional. Built-in defaults live in `config.py`.

- `config.yaml` sets chains, poll interval, safety thresholds, alert rules, and
  per-host rate limits.
- `accounts.yaml`, `signals.yaml`, `channels.yaml`, `smart_wallets.yaml` and
  `fomo_wallets.yaml` are placeholders for later phases.

## Layout

```
main.py            CLI entry point (run / --dry-run / check / health)
pipeline.py        sighting -> resolve -> safety -> verdict -> alert
config.py          config + secrets loading, log redaction
db.py              SQLite state with versioned migrations
net.py             polite HTTP: per-host rate limits, backoff, never raises
extract.py         CA extraction
safety.py          DexScreener / RugCheck / GoPlus checks
scoring.py         verdicts (full scoring in phase 2)
alerts.py          Discord / Telegram formatting and delivery
dryrun.py          mocked HTTP transport serving sample_data/
sources/           dex_source.py (phase 1); telegram/x/fomo to come
backtest/          phase 7
sample_data/       fake API responses + messages for --dry-run
tests/             pytest suite
```

## Notes and caveats

- **API shapes.** The DexScreener (`/token-profiles/latest/v1`,
  `/latest/dex/tokens/…`, `/latest/dex/pairs/…`), RugCheck
  (`/v1/tokens/{mint}/report`) and GoPlus (`/api/v1/token_security/{chain_id}`)
  integrations follow their public API formats. The parsers are defensive, so a
  missing field becomes "unknown" rather than a crash. Run `python main.py check
  <some known CA>` once with real network access and confirm the output looks right.
- **Rate limits.** Default rate limits sit under each provider's published free
  limits. You can change them per host in `config.yaml`.
- **Before trusting any calls:** leave the bot in alert and paper-trading mode for
  weeks, and read the `qualify` report (phase 7) before putting real money behind
  anything it says.
