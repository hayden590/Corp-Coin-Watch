# corp-coin-watch

Finds new meme-coin contract addresses (CAs) on X, Telegram, DexScreener and
Fomo. It checks whether each coin is safe and legit, analyses the chart,
on-chain activity, social connections and chatter, scores it, and alerts you
on Discord and/or Telegram. It records what actually happened to every coin,
backtests and paper-trades strategies, and tells you plainly whether it has an
edge.

> **This bot never buys or sells anything.** It has no wallet keys, no signing
> code and no trading path, and a test fails the build if that kind of code ever
> appears. Alerts and paper trading (fake money) only. Nothing it says is
> financial advice.

The full design is in [SPEC.md](SPEC.md). All 7 phases are built.

## Quick start

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                  # fill in what you have; everything is optional

python main.py --dry-run              # see the whole thing work on sample data first
```

The dry run needs no keys and no network. It walks through a scripted story:

- **Step 1** – coins arrive from DexScreener, X and Telegram and get alerts:
  - a good coin backed by a trader
  - an official company coin
  - a honeypot
  - a mint-authority rug
  - a signal account that just changed its name
  - a "narrative" coin riding a celebrity tweet, with a prompt-injection attempt in the chatter
  - a low-activity coin (filtered) and a wash-traded coin (warned)
  - a pump.fun coin whose safety data is missing
- **Step 2** – 15 minutes later, source tweets are re-fetched. The official coin
  becomes **VERIFIED**; the signal account's deleted tweet is caught.
- **Step 3** – liquidity is pulled on an alerted coin, so an **EXIT WARNING**
  fires and the paper trade closes.
- **Step 4** – `backtest` and `qualify` reports on *synthetic* history, just so
  you can see what they look like.

## Setting up the real sources

Everything is optional. Each source you leave out is simply skipped, and `python main.py health` shows what's on.

| What | How |
|---|---|
| **Discord alerts** | Server Settings → Integrations → Webhooks → New Webhook → `DISCORD_WEBHOOK_URL` |
| **Telegram alerts** | [@BotFather](https://t.me/BotFather) `/newbot` → `TELEGRAM_BOT_TOKEN`. Message your bot, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy `chat.id` → `TELEGRAM_CHAT_ID` |
| **Telegram channels (reading)** | https://my.telegram.org → API tools → `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`. List channels in `channels.yaml`, then run `python main.py telegram-login` once (asks for your phone + code; the session is saved in `data/`) |
| **X** | Use **burner accounts only**. Put them in `.env` as `X_ACCOUNTS=user:pass:email:email_pass;...` or `X_COOKIES=user=cookie_string;...`, then run `python main.py x-login` |
| **Solana wallets + global fees** | Free Helius key → `HELIUS_API_KEY` |
| **EVM wallets** | Ethereum/Base work with no key (Blockscout). BSC etc. need a free `ETHERSCAN_API_KEY` |
| **AI text analysis** | Set `text.backend` in `config.yaml` to `claude` (needs `ANTHROPIC_API_KEY`; defaults to the small, cheap `claude-haiku-4-5`) or `ollama` (free, local) |
| **Fomo** | fomo.family has **no public API** (its app uses a private, login-gated backend). By default the bot uses the wallets in `fomo_wallets.yaml`. If you have a JSON endpoint you're allowed to use, set `fomo.leaderboard_url` / `fomo.theses_url` in `config.yaml` |

### Desktop pop-ups (click to open the coin)

With `alerts.desktop: true` (the default), each alert also pops up as a notification on your laptop.
Where it appears is decided by your OS: bottom-right on Windows, top-right on macOS.

- **Windows 10/11:** included in `pip install -r requirements.txt`.
- **macOS:** also run `brew install terminal-notifier`. Without it you still get pop-ups, but you can't click through.
- **Try it:** `python main.py test-notify`, then click the notification.

Clicking a **VERIFIED / UNCONFIRMED** alert opens `alerts.buy_link` in `config.yaml`, e.g. your Fomo
token page (use `{chain}` and `{address}` in the link). If that's empty, it opens DexScreener.
**DANGER / UNCHECKED alerts never open a buy page**, only the chart. Exit warnings open the coin so you
can sell. You still press buy or sell yourself; the bot never trades.

### Alerts on your phone too

The bot runs on one computer. Alerts can go to your laptop (pop-ups) **and** your phone at the same time:
set up the Telegram bot (or a Discord webhook) and install that app on your phone to get push notifications.
Telegram alerts come with tap-able **🛒 Open to buy** and **📈 Chart** buttons. The buy button only appears on
VERIFIED/UNCONFIRMED coins, and exit warnings get **💸 Open to sell**.

### Run it on a server (so your computer can be off)

A small cloud server runs the bot 24/7. **Free:** Google Cloud's always-free *e2-micro*. It must be in
`us-west1`, `us-central1` or `us-east1`, with a *Standard* persistent disk of 30 GB or less, and the setup script adds swap for its
1 GB RAM. **Paid:** roughly $4–6/month. Good choices are a Hetzner CX22, a DigitalOcean Basic
droplet or a Vultr Regular instance. Pick **Ubuntu 24.04**, the cheapest size (1–2 GB RAM is plenty) and a region near you.

1. Create the server and log in: `ssh root@<server-ip>`
2. Get the setup script and run it. The repo is private, so either use a GitHub
   [fine-grained token](https://github.com/settings/tokens?type=beta) with read access to this repo, or add the
   server's SSH key as a deploy key:
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/<you>/Corp-Coin-Watch/<branch>/deploy/setup-server.sh  # or scp it
   sudo bash setup-server.sh https://<token>@github.com/<you>/Corp-Coin-Watch.git <branch>
   ```
3. Put your keys in `/opt/corp-coin-watch/.env` (`sudo nano /opt/corp-coin-watch/.env`).
4. If you use them, do the one-time logins on the server:
   `cd /opt/corp-coin-watch && sudo -u ccw .venv/bin/python main.py telegram-login` (and `x-login`).
5. Start it with `sudo systemctl start corp-coin-watch`. From then on it starts on boot and restarts itself if it crashes.
6. Watch the live log with `sudo journalctl -u corp-coin-watch -f`, check status with `sudo -u ccw .venv/bin/python main.py health`,
   and get the latest code with `sudo bash /opt/corp-coin-watch/deploy/update.sh`.

A server has no screen, so its own desktop pop-ups are switched off (`config.local.yaml`). Alerts reach your
laptop and phone like this instead.

### Pop-ups on your laptop and phone from the server (ntfy)

[ntfy](https://ntfy.sh) is a free push service. The server publishes each alert to your own private "topic", and
anything subscribed to that topic shows it as a pop-up. Clicking the pop-up opens the coin: your buy page for
VERIFIED/UNCONFIRMED coins, the chart for DANGER/UNCHECKED ones. Your phone doesn't need your laptop to be on.

1. Make a private topic with `python main.py new-ntfy-topic`, then put `NTFY_TOPIC=...` in the server's `.env` and restart the bot.
2. **Phone:** install the **ntfy** app, tap **+**, and enter your topic.
3. **Laptop**, pick one:
   - **No install:** open https://ntfy.sh/app, choose *Subscribe to topic*, enter your topic, and allow notifications.
   - **Native pop-ups:** copy this repo to the laptop, run `pip install -r requirements.txt` (plus `brew install terminal-notifier` on a Mac),
     then run `python laptop_notifier.py <your-topic>`. It shows alerts only while it's running.
4. Test it with `python main.py test-notify` on the server.

The topic name works like a password, because anyone who knows it can read your alerts. Keep it long and random, which is what
`new-ntfy-topic` generates. Telegram and Discord alerts still work alongside ntfy if you want them too.

Then list the accounts and wallets you care about. All of these files are optional:

- `signals.yaml` – influential X accounts, **by numeric user ID**, with a tier (1 = mega public figures, 2 = top traders), a weight and optional known wallets. Also takes `force_include` / `force_block` lists.
- `accounts.yaml` – official org accounts and their domains.
- `channels.yaml` – Telegram channels, with a label and weight.
- `smart_wallets.yaml`, `fomo_wallets.yaml` – wallets to track.

Get an account's user ID from its profile via any "X user ID lookup" site. The bot never matches on display names.

## Commands

```bash
python main.py                    # run everything (Ctrl+C to stop)
python main.py --dry-run          # full pipeline on sample data
python main.py check "<text>"     # check a CA / tweet / link right now (add --send to alert)
python main.py health             # every source, each X account, requests this hour, AI backend
python main.py leaderboard        # best and worst CA-posting X accounts
python main.py backtest           # replay strategies on recorded history
python main.py qualify            # paper-trading report card ("NO EDGE FOUND" if it fails)
python main.py new-ntfy-topic     # private channel for phone + laptop pop-ups
python main.py test-notify        # send a sample alert
python -m pytest                  # 174 tests
```

## How a coin is judged

1. **Found.** CAs are pulled from text and from links (DexScreener, pump.fun,
   Birdeye, Etherscan-family, Solscan). The extractor handles EVM `0x`+40 hex and
   Solana base58 that decodes to 32 bytes, and ignores tx hashes, quote tokens and
   zero-width tricks. Every sighting is stored with its source and first-seen time.
2. **Safety** (DexScreener + RugCheck/GoPlus): liquidity, pair age,
   mint/freeze authority, LP lock, top-10 holders, honeypot, taxes and owner
   controls. If an API fails the check reads "unknown". It is never treated as a pass.
3. **Global fees (Solana).** Total fees traders actually paid (base + priority +
   Jito tips).
   - Below `min_global_fees_sol` (1.5) means low activity, so no alert.
   - High volume with tiny fees means a "likely wash volume" warning.
   - These are filters only: they never override DANGER and never prove a coin is safe.
4. **Legitimacy.**
   - The source tweet is re-fetched after 15 min. If it was deleted → DANGER.
   - Any profile change on the poster in the last 72h → DANGER.
   - The exact CA is searched for on the official website.
   - A first-time CA poster is flagged.
   - A "narrative coin" (name matches a tier-1 tweet with no tier-1 engagement) is flagged HIGH RISK.
5. **Backing.**
   - Signal-account endorsements (tier 1 only counts for direct engagement with the CA).
   - Distinct Telegram channels.
   - Smart-wallet buys, weighted by each wallet's own track record. A wallet needs 10 resolved trades before it counts.
6. **Connections.** Who is really behind the token. A linked account only counts
   if it posted the CA itself or has it in its bio or pinned tweet. Otherwise it's a
   "possible fake link". The bot also checks:
   - the deployer's past launches
   - tier-1/2 follows and recent interactions
   - follower quality
   - bought/hijacked-account signs (renames, mass-deleted tweets, recently turned crypto)
7. **Chart** (GeckoTerminal, Birdeye fallback): momentum, volume, buy/sell ratio,
   structure, distance from ATH, and an entry-quality label.
8. **Chatter.** Posts are rated by Claude/Ollama. All scraped text is treated as
   untrusted data: it's fenced off in the prompt, and the model output is
   schema-checked and can only nudge a minor score.
9. **Verdict.**
   - **DANGER — DO NOT BUY**: anything above that fails.
   - **UNCHECKED**: safety couldn't be verified.
   - **VERIFIED**: the official site lists the CA, the tweet is still up, and safety passed.
   - **UNCONFIRMED**: otherwise.

   Nothing overrides DANGER. Only backing or an official confirmation can trigger
   an alert; connection, chart and text scores can't on their own.
10. **Follow-up.** For 48h after an alert, the bot sends **EXIT WARNING** alerts
    for any of these:
    - dev selling
    - top holders selling
    - liquidity pulled
    - smart wallets exiting
    - holders dropping
    - buys fading while price is still up

## Learns by itself (no lists needed)

- **Smart wallets from any trading app.** The bot reads the blockchain, so it covers traders on every
  app (Fomo, Photon, Axiom, BullX, …).
  - When a coin it watched runs (+100% at 1h/6h, no rug), it records who bought *early*. It skips the
    snipe bots in the first 60 seconds.
  - It keeps the best 15 of those wallets and watches what they buy next.
  - A wallet only counts toward alerts once it has a real track record (10 resolved trades).
  - Needs `HELIUS_API_KEY`. The free plan allows about 300 calls a day, which the bot enforces.
- **Fomo leaderboard traders.** With a free `FOMO_API_KEY` from [fomoapi.io](https://fomoapi.io), the bot follows
  Fomo's top traders' wallets. It looks up each trader once, so it stays within the free credits. Check the
  connection with `python main.py fomo-test`.
- **AI picks.** Every day the bot retrains a model on everything it has seen, always tested on newer coins
  it didn't train on.
  - Only if that test is good enough (AUC ≥ 0.6, 150+ coins) will it alert on its own. The model must rate
    the coin at 65% or more *and* there must be a hard signal: a trusted wallet buying, some backing, or a
    decent chart.
  - Expect this to take a few weeks of history.
- **X chatter.** For promising coins it searches X for the exact contract address, rates what people
  say, and learns which accounts call coins early. Needs X burner accounts (`x-login`).

## Learning and the edge test

- **Outcomes.** Every coin is snapshotted when first seen (all features, scores and
  verdict). Its outcome is recorded at 15m, 1h, 6h and 24h: max gain, max
  drawdown, and whether it rugged.
- **`backtest`**
  - Replays the `strategies:` from `config.yaml` with fees and slippage.
  - Always splits by **time**: it trains on older coins and tests on newer ones.
  - Reports win rate, avg win/loss, max drawdown and EV per trade.
  - Shows which signals actually predicted outcomes.
  - Finds the best global-fees thresholds.
  - Optionally fits an ML model (logistic regression / gradient boosting).
  - Warns loudly about overfitting.
- **Paper trading.** Runs the same strategies live with fake money. Entries are at the
  alert price plus slippage. Exits happen on target, stop, time limit, an exit
  warning, or a DANGER flip.
- **`qualify`** grades each strategy against `config.yaml`'s criteria (default: 100
  paper trades over 3+ weeks, positive EV after fees, drawdown under 30%). If
  any criterion fails it says **NO EDGE FOUND**, retrains, and keeps paper trading.
  It never switches to real trading.

**Leave it running in paper mode for weeks and read `qualify` before trusting any call.**

## Layout

```
main.py            CLI (run / --dry-run / check / health / leaderboard / backtest / qualify / logins)
monitor.py         supervised loops: every source + background jobs, isolated from each other
pipeline.py        sighting -> resolve -> safety -> fees -> legitimacy -> wallets -> connections
                   -> chart -> text -> score -> snapshot -> alert -> paper trade
extract.py         CA extraction            safety.py      DexScreener / RugCheck / GoPlus
fees.py            global fees paid          verify.py      legitimacy checks
discovery.py       X scorecards, tiers       wallets.py     smart wallets + dump flag
graph.py           connections analysis      charts.py      OHLCV features + exit flags
text_analysis.py   AI chatter rating         scoring.py     scores, verdicts, alert gating
alerts.py          Discord / Telegram        papertrade.py  fake-money trades + qualify
backtest/          outcomes.py, engine.py    sources/       dex, x, telegram, fomo
db.py              SQLite + migrations       net.py         polite HTTP (rate limits, backoff)
dryrun.py          offline demo harness      sample_data/   fake API responses
```

## Caveats

- **Unverified API shapes.** This was built in a sandbox that couldn't reach
  DexScreener, RugCheck, GoPlus, Helius, GeckoTerminal, pump.fun or fomo.family.
  The integrations follow those services' public formats, and every parser treats
  surprises as "unknown" rather than crashing. Run
  `python main.py check <a CA you know>` first and compare against the websites.
- **Unofficial APIs.** pump.fun's frontend API and twscrape are unofficial and can
  break. The bot logs the failure, alerts once, and keeps running.
- **X terms of service.** Scraping X with burner accounts is against X's terms.
  Those accounts can be locked, so never use your main account.
- **Free tiers.** Stay inside free limits. The per-host rate limits are in
  `config.yaml`, and the X hourly budget is `x.requests_per_hour`.
