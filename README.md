# funding-scanner

Read-only archive of perpetual funding rates, spot prices and dated futures across
16 venues. Collects. Does not trade, holds no keys, moves no money.

The point is not the snapshot. Any Telegram channel can tell you a rate is 60%
right now. The point is **how long it lasts**, because a 60% spread that survives
two hours is a loss after four fills, while a 12% spread that holds for a month is
a business — and nothing but an archive can tell those apart.

## What it collects

| Kind | Fields | Enables |
|---|---|---|
| perps | funding rate, **interval**, mark, index, turnover, OI | funding carry, cross-venue spreads |
| spot | last, turnover | the hedge leg; borrow feasibility |
| dated futures | mark, **expiry**, turnover | calendar basis, perp-vs-quarterly |

All on one timestamp, so the strategy question stays open. Nothing in the collector
assumes which trade you will end up caring about.

**Venues:** binance, bybit, okx, gate, bitget, mexc, kucoin, htx, coinex, bitmex,
bingx, hyperliquid, dydx, paradex, backpack, deribit.

The funding **interval** is read per instrument, never assumed. Hyperliquid, dYdX
and Backpack settle hourly; a third of BingX symbols are 4h. Annualising those at
8h understates them by 2–8x, which is the most common error in cross-venue scans
and the reason small venues look quiet when they are the opposite.

## Setup

1. Create a repository (**public** — Actions minutes are unlimited there, and
   there is nothing secret in here).
2. Copy in `venues.py`, `scan.py`, `analyze.py`, `.github/workflows/scan.yml`.
3. Settings → Actions → General → Workflow permissions → **Read and write**.
4. Actions tab → `scan` → **Run workflow** to trigger the first run by hand.

After that it runs every 30 minutes and commits to `data/`.

## The one thing to check first

**GitHub-hosted runners are US-based, and several exchanges geo-block US IPs**
(451). Binance, Bybit, OKX and BitMEX are the likely casualties. This is not a
bug in the scanner and it is not a market fact — it is a network fact, and it
would be easy to mistake for "there was nothing there".

So the scanner records reachability as data, not as diagnostics:

```
data/venue_status.csv     ts, source, ok, detail    ← written on EVERY run
```

After the first run, look at it. If coverage is poor, two options:

* **Keep Actions**, accept that the on-chain venues (hyperliquid, dydx, paradex,
  backpack) plus whichever CEXs answer are your universe. These are the venues
  with the widest dispersion anyway, so this is not the disaster it sounds like.
* **Move to a €4/month VPS in Germany**, which reaches everything. Same scripts,
  a cron line instead of a workflow:

```
*/30 * * * * cd ~/funding-scanner && /usr/bin/python3 scan.py >> scan.log 2>&1
```

A self-hosted GitHub runner on that VPS also works and keeps the workflow as-is.

## Reading the archive

```bash
python3 analyze.py                          # every section
python3 analyze.py --section persistence    # the one that matters
python3 analyze.py --capital 2000 --threshold 20
```

Sections: coverage · persistence · funding carry · cross-venue · calendar basis ·
paper run.

Everything reports its own sample size. Under two days of history the tool says so
in capitals, because a conclusion drawn from six hours is mostly a measurement of
which afternoon you started on.

## What the numbers are NOT

* **No order book.** Fees come from a price list; real slippage on your size is
  unmeasured. Every figure here is an upper bound.
* **No basis risk.** Cross-venue spreads ignore divergence between the two venues,
  which is the dominant risk in that trade and can exceed the whole spread.
* **The paper run is deliberately naive.** It charges a full round trip on every
  switch — the cost that actually kills bots — and ignores slippage, which
  flatters it. It bounds the opportunity; it is not a strategy.

Treat the output as a shortlist to investigate. Not a signal.

## Suggested path

| Phase | Capital | What you learn |
|---|---|---|
| weeks 1–4 | **$0** | Does anything persist long enough to clear four fills? |
| then | **$1–2k** | Does live execution match what the archive predicted? |
| only if it does | **up to $10k** | Whether the gap between the two is stable |

Each step is paid for by the logs of the previous one, never by confidence. If
phase 1 shows spreads dying inside their break-even hold, you have spent about
$0 and a few evenings to learn something most people pay a deposit for.

## Storage

Roughly 5 MB/day compressed at full coverage. Git deltas it well (each append is
a byte-prefix extension of the previous file), but `analyze.py` holds rows in
memory and gets heavy past a few weeks of full-venue data. When that bites, either
raise `--min-turnover` or move older partitions out of `data/`.

## Files

```
venues.py     adapters — the only place that knows an exchange exists
scan.py       one snapshot -> data/YYYY-MM-DD.csv.gz + venue_status.csv
analyze.py    reads the archive, computes every strategy, decides nothing
```

Python 3.11+, standard library only. No dependencies, no keys, no install.
