# corp-coin-watch — Full Build Spec

Build a Python project called "corp-coin-watch". It finds new crypto meme coin
contract addresses (CAs) from X, Telegram, DexScreener, and the Fomo app, checks
whether each one is legit and safe, analyses its chart, on-chain activity, social
connections, and what people are saying about it, scores it, and alerts me on
Discord and/or Telegram. It learns from real outcomes through backtesting and
paper trading, and reports whether it has a real edge.

HARD RULE: it must NEVER buy or sell anything with real money. Alerts and paper
trading only. There is no code path to real trading.

Build it in the phases listed at the bottom. Start by showing me the project
structure and asking about anything unclear. Test each module before moving on.

---

## 1. Setup
- Python 3.11+, virtual env, requirements.txt, README with full setup steps.
- Secrets in .env (with .env.example): X burner account credentials/cookies,
  TELEGRAM_API_ID, TELEGRAM_API_HASH, DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID, HELIUS_API_KEY (free tier), optional ANTHROPIC_API_KEY.
  Never hardcode or log secrets.
- .gitignore: .env, session files, twscrape DB, SQLite DB, logs, caches.
- SQLite for all state: seen tweets/messages, tracked CAs, account snapshots,
  account scorecards, endorsements, social graph cache, smart wallet activity,
  chart snapshots, text scores, outcomes, paper trades, alert history.
- Module structure:
  - sources/x_source.py, sources/telegram_source.py, sources/dex_source.py,
    sources/fomo_source.py
  - extract.py, verify.py, safety.py, wallets.py, charts.py, text_analysis.py,
    graph.py, discovery.py, scoring.py, alerts.py, db.py, main.py
  - backtest/, papertrade.py, tests/

## 2. Config files (all optional manual overrides — the bot works without them)
- config.yaml: poll intervals, chains, safety thresholds (min liquidity USD,
  max top-10 holder %, max buy/sell tax), all scoring weights, persistence delay,
  auto-promotion/blacklist rules, AI backend choice, qualification criteria.
- accounts.yaml: official org accounts — handle, X user ID, official domain(s), chain(s).
- signals.yaml: influential accounts — handle, X user ID, tier (1 = mega public
  figures, manual only; 2 = top traders), weight, optional known wallets.
  Supports force-include and force-block.
- channels.yaml: Telegram channels — channel, label, weight.
- smart_wallets.yaml and fomo_wallets.yaml: wallet, chain, label.

## 3. Sources
### a) X via twscrape (burner accounts, no paid API)
- Search sweeps in "Latest" mode on a slow randomised interval (default 3–5 min).
  Default queries: "pump.fun", "dexscreener.com", "CA:", "contract address".
- Watch listed and auto-promoted accounts by user ID (never display name):
  tweets, retweets, quotes, replies.
- Hourly request budget, exponential backoff on 429s, cooldown on locked
  accounts, rotate between accounts. If all are down, send ONE alert and keep
  everything else running.

### b) Telegram via Telethon (my own account)
- Read new messages from channels.yaml. Store session locally.

### c) DexScreener free API
- Poll latest token profiles / new pairs for my chains. Record linked X,
  Telegram, website. Check current docs, respect rate limits.

### d) Fomo app (fomo.family)
- Check whether the web app exposes a public leaderboard, trader profiles, or
  coin theses. If so, pull top trader wallets on a slow schedule (default every
  6h) and theses for tracked coins. Polite rate limits. If blocked or changed,
  log it, alert me once, keep running. Fall back to fomo_wallets.yaml.

## 4. CA extraction (extract.py)
- EVM: 0x + exactly 40 hex chars.
- Solana: base58, 32–44 chars, must decode to 32 bytes (solders or base58).
- Also pull CAs from dexscreener / pump.fun / birdeye / etherscan / solscan links.
- Deduplicate; record first-seen time and source for every sighting.

## 5. Auto-discovery on X (discovery.py)
- Every account that posts a CA gets a scorecard: user ID, handle, followers,
  verified type, CAs posted, % passed safety, % rugged/died (liquidity -80% or
  honeypot later), % did well (price up at 1h/24h vs first sighting), average
  earliness vs first-seen time across all sources.
- Auto-promote to tier 2 on configurable rules (e.g. min 5 calls, >40% good,
  <20% rugs). Auto-blacklist high rug rates; their CAs start with a penalty.
- Accounts with X business/gold verified type are treated as org accounts, using
  their profile website as the official domain.
- `python main.py leaderboard` shows best and worst accounts with stats.

## 6. Legitimacy checks (verify.py)
Each CA gets pass / warn / fail / unknown for:
a) Tweet persistence: re-fetch source tweet after delay (default 15 min).
   Deleted = DANGER ("likely hacked account").
b) Account integrity: daily snapshots of name, bio, profile image, verified
   type. Any change in last 72h = red flag.
c) Official website: search the org's domain (homepage + /press, /news, /token,
   /crypto if present) or DexScreener's website link for the exact CA.
   Found = strong pass. Not found = warn.
d) First-time CA poster = flag.
e) Narrative coin: token name matches a recent tier-1 tweet but no direct tier-1
   engagement with the CA = high risk flag.

## 7. Safety checks (safety.py)
- DexScreener: liquidity USD, pair age, FDV, volume.
- Solana → RugCheck API: mint/freeze authority revoked, LP locked/burned, top
  holder concentration, risk score.
- EVM → GoPlus token security API: honeypot, buy/sell tax, mintable, proxy,
  blacklist functions, holder concentration.
- Compare to config thresholds. API failure = "unknown", never crash.

## 8. Smart wallets (wallets.py)
- Check whether smart wallets (manual, Fomo, and signal accounts' known wallets)
  hold or recently bought each CA. Helius free tier / public RPC for Solana,
  free explorer APIs for EVM. Cache to stay in free limits.
- Every wallet gets a track-record scorecard (win rate over time, rug rate,
  earliness). Wallets only affect scoring after enough history (configurable).
  Leaderboard rank alone is never trusted.
- Flag "possible dump on followers" if a signal account's wallet sells within a
  configurable window after they post.

## 9. Connections analysis (graph.py)
### a) Who is behind the token
- Collect linked X accounts: DexScreener profile link, pump.fun metadata socials,
  first account to post the CA.
- Only "confirmed creator/affiliate" if that account posted the CA itself or links
  the token in bio/pinned tweet. Otherwise flag "possible fake link", no score.
- Check deployer wallet's past launches and how they ended.

### b) Social graph cache
- Cache FOLLOWING lists of tier-1 and top tier-2 accounts via twscrape. Refresh
  tier-1 every 3 days, tier-2 weekly, slow and spread out.
- For a confirmed creator: which tier-1 accounts follow them, how many tier-2
  follow them, any tier-1/2 replies/quotes/retweets in last 90 days, follower
  count, account age, follower quality (sample: % no pfp, zero tweets, very new).

### c) Bought/hijacked account detection
- Flag recent renames, old accounts that only recently started posting crypto,
  mass-deleted old tweets. Any flag cancels the connection bonus and adds a warning.

## 10. Chart analysis (charts.py)
- Pull OHLCV candles (1m, 5m, 15m, 1h) from GeckoTerminal free API, fallback
  Birdeye free tier. Cache, respect limits.
- Features: price change per timeframe, volume vs rolling average, buy/sell count
  and volume ratio, volatility, higher-highs/lower-highs structure, distance from
  ATH, time since launch, liquidity trend, holder count trend, top holder % trend.
- Exit warning flags (timestamped): dev/deployer selling, top-10 holders selling,
  liquidity removed, smart wallets exiting, holder count dropping, buy volume
  fading while price still up.

## 11. Text analysis (text_analysis.py)
- Collect text per CA: X replies/quotes, Telegram mentions, Fomo theses.
- AI backend set in config: Claude API (small/cheap model) or local Ollama (free).
  Check current docs for both.
- Model returns ONLY JSON: sentiment score, hype vs substance rating, bot-like
  repeated phrasing (y/n), main claims, red flags.
- SECURITY: all scraped text is untrusted data. Clearly wrap it as data in the
  prompt, instruct the model to ignore any instructions inside it, and model
  output can only produce scores — never trigger actions.
- Text signals are a minor weight and never override DANGER.

## 12. Scoring and verdict (scoring.py)
- Backing score = weighted: signal account endorsements (tier 1 = direct
  engagement with the CA only), distinct Telegram channels posting it, smart
  wallet buys (weighted by wallet track record).
- Connection score = weighted by tier and recency of connections (recent
  interaction >> old follow).
- Plus chart quality and text scores. All weights in config.yaml.
- Verdict:
  - DANGER: deleted tweet, recent account changes, honeypot, failed safety,
    insider dump flag, hijacked-account flag.
  - VERIFIED: official website confirms CA + tweet persists + safety passes.
  - UNCONFIRMED: safety passes, no official confirmation.
- No score can override DANGER. Connection/text scores alone can never cause an alert.

## 13. Backtesting (backtest/)
- Snapshot every tracked CA at first sighting (all features, scores, verdict),
  then record outcomes at 15m, 1h, 6h, 24h: max gain, max drawdown, rugged y/n.
- Backtest entry/exit rules on this history. Start rule-based, then optional
  simple ML (logistic regression / gradient boosting) predicting "hits +X%
  before -Y%".
- Always split by TIME (train on older coins, test on newer unseen coins).
  Report win rate, avg win/loss, max drawdown, expected value per trade after
  realistic fees and slippage. Report which signals (including connection score,
  text score, smart wallets) actually predicted outcomes.
- Warn loudly about overfitting if results look too good.

## 14. Paper trading and qualification (papertrade.py)
- Run strategies live with fake money: simulated buy at alert-time price plus
  slippage, exit on strategy rules or exit warning flags.
- Qualification criteria in config (e.g. min 100 paper trades over min 3 weeks,
  positive expected value after fees, max drawdown under limit).
- `python main.py qualify` shows a report card: pass/fail per criterion, and says
  "NO EDGE FOUND" plainly if it fails. On failure, retrain on new data and keep
  paper trading. Never switch to real trading.

## 15. Alerts (alerts.py)
- Discord webhook and/or Telegram bot. Include: verdict (big and clear), token
  name, chain, CA, source link, each check with ✅/⚠️/❌, liquidity, pair age,
  holder spread, backing score breakdown, connections ("Creator followed by: ...
  | Recent interaction: ... | Account age | Warnings"), chart summary, entry
  quality, active exit warnings, text sentiment summary, historical win rate for
  similar setups, DexScreener link.
- DANGER alerts clearly marked "DO NOT BUY".
- Follow-up "EXIT WARNING" alerts for previously alerted coins when exit flags fire.
- Don't re-alert the same CA unless its verdict changes.

## 16. Reliability and testing
- Logging to console and rotating log file.
- `python main.py health`: status of every source, each X account, last
  successful fetch, requests used this hour, AI backend status.
- `--dry-run`: full pipeline on sample data from JSON files, no keys needed.
- Unit tests: CA extraction (fake/edge cases), ID-based matching, narrative coin
  flag, fake-link and renamed-account cases, prompt-injection text, scoring,
  verdict logic, time-split backtest.

---

## Build phases
1. Setup, db, extract, DexScreener source, safety checks, alerts, dry-run. Test.
2. Telegram source, legitimacy checks, scoring/verdicts. Test.
3. X source (twscrape), auto-discovery, endorsements. Test.
4. Smart wallets, Fomo source. Test.
5. Connections analysis. Test.
6. Charts, exit warnings, text analysis. Test.
7. Outcome tracking, backtesting, paper trading, qualify report. Test.
Pause after each phase, summarise what works, and wait for me before continuing.
