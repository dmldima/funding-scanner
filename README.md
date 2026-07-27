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

**Venues:** binance, bybit, okx, gate, bitget, mexc, kucoin, htx, whitebit,
coinex, bitmex, bingx, hyperliquid, dydx, paradex, backpack, deribit.

### The funding interval

Annualising a 4h rate as if it were 8h understates it by 2x, and an hourly rate
by 8x. This is the most common error in cross-venue scans and the reason small
venues look quiet when they are the opposite — so the interval is read per
instrument wherever the venue publishes it, which is nearly everywhere:

| | venues |
|---|---|
| read per instrument | binance, bybit, gate, kucoin, bingx, mexc, bitget, bitmex, whitebit |
| derived from the settlement timestamps | okx, coinex |
| fixed hourly, and genuinely are | hyperliquid, dydx, backpack |
| **assumed 8h — no endpoint publishes it** | **htx, paradex, deribit** |

It is worth knowing how little of the market is actually on 8h. Across a live
snapshot: **2401 instruments settle every 4h, 2345 every 8h, 302 hourly.** Over
half of MEXC (547 of 1030) and of Bitget (373 of 721) are 4h contracts, both of
which this scanner annualised at 8h until the intervals were read — halving the
recorded APR on the two largest mid-tier venues.

The three still assumed at 8h are a known limit, not a verified fact.

## Setup

1. Create a repository (**public** — Actions minutes are unlimited there, and
   there is nothing secret in here).
2. Copy in `venues.py`, `scan.py`, `analyze.py`, `.github/workflows/scan.yml`.
3. Settings → Actions → General → Workflow permissions → **Read and write**.
4. Actions tab → `scan` → **Run workflow** to trigger the first run by hand.

After that it runs on a schedule and commits to `data/`.

### The scheduler will not keep time, and that is the reason to leave

GitHub's `schedule:` trigger is best-effort. Runs queue behind every other
repository's and are dropped outright under load — it is documented, and it is
not subtle. Measured here over eight hours with `*/30`:

```
expected 16 runs        actual 2        largest gap 256 minutes
```

The workflow now fires at four odd minutes past the hour instead of `*/30`,
which avoids the platform-wide stampede at `:00` and `:30` and gives four
chances to land two runs. It helps. It does not make the scheduler a cron.

This matters more than the geo-block. `analyze.py` handles gaps honestly — a
missing snapshot breaks the run rather than being credited as four hours of a
persisting spread — so irregular sampling never produces a *wrong* number. It
produces a number with nothing in it. Measuring how long an opportunity survives
requires sampling at a known cadence, and that is the entire question the
archive exists to answer.

A €4/month VPS running `crontab` fires every 30 minutes, every time. That is the
fix; the staggered schedule is a stopgap.

## The one thing to check first

**GitHub-hosted runners are US-based, and several exchanges geo-block US IPs.**
In practice Binance answers 451 and Bybit 403; OKX and BitMEX, the other two
usual suspects, do answer. That leaves 15 of 17 venues. This is not a bug in the
scanner and it is not a market fact — it is a network fact, and it would be easy
to mistake for "there was nothing there".

So the scanner records coverage as data, not as diagnostics:

```
data/venue_status.csv     ts, source, ok, detail    ← written on EVERY run
```

It logs two different things, and the distinction matters. A **failure** (`ok=0`)
means the venue was unreachable. A **cap** (`ok=1`, detail beginning `CAPPED`)
means the scanner chose to sample: OKX prices funding one instrument per call,
so 150 of its 411 linear swaps are fetched, ranked by turnover. Without that
line the archive would show 150 OKX perps and a later coverage analysis would
read a sampling decision as the size of the market. Raise it with `--okx-cap`,
at roughly 0.3s per instrument.

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
python3 analyze.py --min-leg-turnover 20    # stricter: both spread legs liquid
```

Sections: coverage · persistence · funding carry · cross-venue · calendar basis ·
paper run.

Everything reports its own sample size. Under two days of history the tool says so
in capitals, because a conclusion drawn from six hours is mostly a measurement of
which afternoon you started on.

### Three filters that decide whether a ranking means anything

Each of these, left off, puts a phantom at the top of a table that looks screened.

**The 10.95% floor.** Venues fall back to an interest-rate baseline when the
premium is negligible, and **61% of perps sit exactly there** at any moment
(whitebit 83%, gate 78%, kucoin 74%). It is the venue saying nothing is
happening, so a spread against it is a real rate minus a constant, not a trade.
Excluded from cross-venue by default; `--include-baseline` restores it.

**Both legs, not one.** `--min-leg-turnover` (default $5M) applies to each side.
Screening one leg ranks spreads whose other side cannot be entered at size.

**Tickers shared by different assets.** WhiteBIT's `CAT_PERP` is Caterpillar Inc.
at $883; `CAT` elsewhere is a memecoin at $0.0000014. Contract multipliers are
always exact powers of ten, so a price ratio that is *not* one means two
different assets, and the odd leg is dropped. Four of 748 multi-venue bases
collide this way.

Unknown liquidity is reported rather than hidden: venues that publish no volume
are exempt from `--min-turnover` entirely, so the count and the venues are
printed, and `--drop-unknown-turnover` excludes them.

## What the numbers are NOT

* **No order book.** Fees come from a price list; real slippage on your size is
  unmeasured. Every figure here is an upper bound.
* **No basis risk.** Cross-venue spreads ignore divergence between the two venues,
  which is the dominant risk in that trade and can exceed the whole spread.
* **Annualised rates on hourly phenomena.** −1800% APR is −0.207% per hour. It is
  a rate of accrual while it lasts, not a return you will collect — and whether
  it lasts long enough to clear four fills is what the archive exists to answer.
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
tests/        symbol normalisation and percentile maths
```

```bash
python3 -m unittest discover -s tests
```

Python 3.11+, standard library only. No dependencies, no keys, no install.

`base_asset()` carries most of the tests because it is where a mistake is
invisible: a symbol that normalises wrong does not raise, it silently stops
matching its counterpart on another venue and drops out of a section that still
reports itself as complete. It has produced four distinct silent-corruption bugs
so far — `kPEPE` uppercased into `KAVA`, `TUSDUSDT` collapsed to `T`, OKX's
`_UM` contracts, and Caterpillar pooled with a memecoin — and each one is now a
regression test.
