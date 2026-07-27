#!/usr/bin/env python3
"""Read the accumulated archive and answer the questions a snapshot cannot.

A single scan tells you what a rate IS. Only the archive tells you how long it
LASTS — and persistence is what decides everything, because a 60% spread that
survives two hours is a loss after four fills, while a 12% spread that holds for
a month is a business.

Sections:
  0. Coverage        — what the archive actually contains
  1. Persistence     — how long opportunities survive above a threshold
  2. Funding carry   — single venue, perp funding hedged with spot
  3. Cross-venue     — perp vs perp, and which venues supply the dispersion
  4. Calendar basis  — dated futures vs spot, annualised to expiry
  5. Paper run       — what a naive always-take-the-best rule would have earned

Nothing here trades. It reports, and it reports its own sample size so a
conclusion drawn from six hours of data is visibly that.

Usage:
    python3 analyze.py
    python3 analyze.py --capital 10000 --min-turnover 1
    python3 analyze.py --section persistence
"""

from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PERP, SPOT, FUTURE = "perp", "spot", "future"

# Perp taker fee per side. A cross-venue round trip is four fills.
TAKER_FEE_PCT = {
    "binance": 0.050, "bybit": 0.055, "okx": 0.050, "bitget": 0.060,
    "gate": 0.050, "mexc": 0.020, "kucoin": 0.060, "htx": 0.040,
    "bitmex": 0.075, "coinex": 0.050, "bingx": 0.050, "deribit": 0.050,
    "whitebit": 0.055,   # from /api/v4/public/markets, uniform across 303 perps
    "hyperliquid": 0.045, "dydx": 0.050, "paradex": 0.030, "backpack": 0.050,
    # Published taker schedules, not read from an API — verify before sizing.
    "aster": 0.035, "phemex": 0.060, "kraken": 0.050, "extended": 0.025,
}
DEFAULT_FEE = 0.055
SPOT_FEE_PCT = 0.10
# One spot+perp round trip: buy and sell the spot, open and close the perp.
# Derived rather than written out, because a literal here silently stops
# agreeing with TAKER_FEE_PCT the moment a fee is edited.
ROUND_TRIP_PCT = SPOT_FEE_PCT * 2 + DEFAULT_FEE * 2
RISK_FREE_PCT = 3.81   # 3M T-bill; refresh from federalreserve.gov/releases/h15

# The interest-rate floor every major venue falls back to when the premium is
# negligible: 0.01%/8h == 0.005%/4h == 0.00125%/1h. All three are the same
# number per hour and all three annualise to exactly 10.95% APR, which is why
# "11.0%" appears everywhere in an unfiltered ranking.
#
# 61% of perps sit exactly here at any given moment (whitebit 83%, gate 78%,
# kucoin 74%, hyperliquid 70%). It is not a market signal — it is the venue
# saying nothing is happening — so a "spread" against it is a real rate minus
# a constant, not a trade.
FUNDING_BASELINE_PER_H = 1.25e-5


@dataclass(frozen=True)
class Row:
    ts: str
    venue: str
    kind: str
    symbol: str
    base: str
    expiry: str
    mark: float
    index: float
    funding_rate: float
    funding_interval_h: float
    turnover_musd: float
    oi_musd: float

    @property
    def apr(self) -> float:
        if self.kind != PERP or self.funding_interval_h <= 0:
            return 0.0
        return self.funding_rate * (24 / self.funding_interval_h) * 365 * 100

    @property
    def at_baseline(self) -> bool:
        """The venue is quoting its interest-rate floor, not a market rate.

        Tested per HOUR rather than per interval, which is what makes one test
        cover all of them: 8h, 4h and 1h venues use different raw numbers for
        the same floor. Only the positive floor counts — the same magnitude
        negative is a genuine reading, not a fallback.
        """
        if self.kind != PERP or self.funding_interval_h <= 0:
            return False
        return abs(self.funding_rate / self.funding_interval_h
                   - FUNDING_BASELINE_PER_H) < 1e-9


def _f(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load(data_dir: Path, min_turnover: float,
         drop_unknown_turnover: bool = False) -> list[Row]:
    files = sorted(data_dir.glob("*.csv.gz"))
    if not files:
        print(f"No data in {data_dir}/ — run scan.py first.", file=sys.stderr)
        sys.exit(1)
    rows: list[Row] = []
    unknown: dict[str, int] = defaultdict(int)
    for path in files:
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
                for rec in csv.DictReader(fh):
                    # Concatenated gzip members repeat no header, but a re-created
                    # file might; skip any row that is obviously the header again.
                    if rec.get("ts") == "ts":
                        continue
                    turnover = _f(rec.get("turnover_musd", ""))
                    # A reported 0 means the venue published no volume for that
                    # instrument, not that nobody trades it, so these are kept by
                    # default — dropping them would bias the archive against the
                    # small venues this project exists to examine.
                    #
                    # But keeping them silently is worse: they are exempt from
                    # min_turnover, so a liquidity filter does not apply to them
                    # at all and they can dominate a table the user believes was
                    # screened. They are counted here and reported by the caller.
                    if rec.get("kind") == PERP and turnover <= 0:
                        unknown[rec.get("venue", "?")] += 1
                        if drop_unknown_turnover:
                            continue
                    elif (rec.get("kind") == PERP and turnover > 0
                            and turnover < min_turnover):
                        continue
                    rows.append(Row(
                        rec["ts"], rec["venue"], rec["kind"], rec["symbol"],
                        rec["base"], rec.get("expiry", ""),
                        _f(rec.get("mark", "")), _f(rec.get("index", "")),
                        _f(rec.get("funding_rate", "")),
                        _f(rec.get("funding_interval_h", "")),
                        turnover, _f(rec.get("oi_musd", ""))))
        except (OSError, EOFError, csv.Error) as exc:
            print(f"  ! unreadable, skipped: {path.name}: {exc}", file=sys.stderr)

    if unknown and min_turnover > 0 and not drop_unknown_turnover:
        total = sum(unknown.values())
        top = ", ".join(f"{v}={n:,}" for v, n in
                        sorted(unknown.items(), key=lambda x: -x[1])[:5])
        print(f"  NOTE: {total:,} perp rows report no turnover and are therefore "
              f"NOT screened by\n        --min-turnover {min_turnover:g}. "
              f"Their liquidity is unverified: {top}."
              f"\n        Re-run with --drop-unknown-turnover to exclude them.",
              file=sys.stderr)
    return rows


def by_timestamp(rows: list[Row]) -> dict[str, list[Row]]:
    snapshots: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        snapshots[r.ts].append(r)
    return dict(sorted(snapshots.items()))


def fee(venue: str) -> float:
    return TAKER_FEE_PCT.get(venue, DEFAULT_FEE)


def hours_between(a: str, b: str) -> float:
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                   .total_seconds()) / 3600
    except (ValueError, TypeError):
        return 0.0


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile over an already-sorted list.

    `values[int(len(values) * q)]` is not one: at four samples it returns the
    maximum and labels it the 75th, and it overstates at every small n. That
    matters most on a short archive — which is exactly when the sample IS
    small, so the error arrives precisely when the number is least reliable
    and reads as a longer typical episode than the data supports.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def median_gap_hours(stamps: list[str]) -> float:
    """Typical spacing between snapshots. Everything downstream needs this:
    a gap materially larger than it is an outage, not a persisting position."""
    gaps = [hours_between(a, b) for a, b in itertools.pairwise(stamps)]
    gaps = [g for g in gaps if g > 0]
    return statistics.median(gaps) if gaps else 0.5


# --------------------------------------------------------------------------- #
# 0. Coverage
# --------------------------------------------------------------------------- #

def section_coverage(rows: list[Row], snaps: dict[str, list[Row]]) -> float:
    stamps = list(snaps)
    span_h = hours_between(stamps[0], stamps[-1]) if len(stamps) > 1 else 0.0
    median_gap = median_gap_hours(stamps)

    venues = sorted({r.venue for r in rows})
    kinds: dict[str, int] = defaultdict(int)
    for r in rows:
        kinds[r.kind] += 1

    print("=" * 78)
    print("0. COVERAGE")
    print("=" * 78)
    print(f"  snapshots      {len(stamps)}")
    print(f"  span           {span_h:.1f}h ({span_h / 24:.1f} days)")
    print(f"  median gap     {median_gap * 60:.0f} min")
    print(f"  rows           {len(rows):,}  "
          f"({', '.join(f'{k}={v:,}' for k, v in sorted(kinds.items()))})")
    print(f"  venues ({len(venues)})   {', '.join(venues)}")
    if span_h < 48:
        print("\n  WARNING: under two days of history. Persistence numbers below are")
        print("  indicative only — nothing here is a basis for committing capital yet.")
    return span_h


# --------------------------------------------------------------------------- #
# 1. Persistence — the reason the archive exists
# --------------------------------------------------------------------------- #

def section_persistence(snaps: dict[str, list[Row]], threshold: float) -> None:
    """For each (venue, symbol), measure unbroken runs of |APR| above threshold.

    Two corrections that decide whether this number means anything:

    * A pair missing from intermediate snapshots — venue geo-blocked, adapter
      failed, cron skipped — must BREAK the run. Without that, a three-day
      outage reads as a three-day persisting opportunity, which is exactly the
      headline this section exists to produce and exactly the wrong one.
    * A run seen in a single snapshot lasted at least one scan interval, not
      zero hours. Recording it as 0.0h drags the median toward "nothing lasts".
    """
    stamps = list(snaps)
    gap = median_gap_hours(stamps)
    max_gap = gap * 2.5

    series: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for ts, batch in snaps.items():
        for r in batch:
            if r.kind == PERP:
                series[(r.venue, r.symbol)].append((ts, r.apr))

    runs: list[tuple[float, str, str, float, bool]] = []
    for (venue, symbol), points in series.items():
        start: str | None = None
        prev_ts: str | None = None
        peak = 0.0

        for ts, apr in points:
            stale = prev_ts is not None and hours_between(prev_ts, ts) > max_gap
            hot = abs(apr) >= threshold
            if start is not None and (stale or not hot):
                # Floor at one scan interval: an episode seen in a single
                # snapshot lasted at least that, not zero hours.
                runs.append((max(hours_between(start, prev_ts), gap),
                             venue, symbol, peak, stale))
                start = None
            if hot and start is None:
                start, peak = ts, abs(apr)
            elif hot:
                peak = max(peak, abs(apr))
            prev_ts = ts
        if start is not None:
            runs.append((max(hours_between(start, points[-1][0]), gap),
                         venue, symbol, peak, False))

    print("\n" + "=" * 78)
    print(f"1. PERSISTENCE — how long |funding APR| stays above {threshold:.0f}%")
    print("=" * 78)
    if not runs:
        print(f"  no pair exceeded {threshold:.0f}% APR in the whole archive")
        return

    durations = sorted(r[0] for r in runs)
    broken = sum(1 for r in runs if r[4])
    print(f"  episodes       {len(runs)}   (scan interval {gap * 60:.0f} min)")
    print(f"  median         {statistics.median(durations):.1f}h")
    print(f"  75th pct       {percentile(durations, 0.75):.1f}h")
    print(f"  longest        {durations[-1]:.1f}h")
    print(f"  under 4h       {sum(1 for d in durations if d < 4) / len(durations):.0%} "
          f"of episodes")
    if broken:
        print(f"  gap-truncated  {broken} episodes ended at a data gap, not a "
              f"rate change — those durations are lower bounds")

    print("\n  Longest episodes:")
    for hours, venue, symbol, peak, was_broken in sorted(runs, reverse=True)[:8]:
        flag = "  (gap)" if was_broken else ""
        print(f"    {venue:<12}{symbol:<18}{hours:>7.1f}h   peak {peak:>8.1f}% APR{flag}")

    # Hours of funding needed to pay one spot+perp round trip. Both terms are
    # in percent, so the APR is divided by hours-per-year and nothing else.
    median_h = statistics.median(durations)
    print(f"\n  Break-even hold vs the median episode ({median_h:.1f}h), "
          f"at a {ROUND_TRIP_PCT:.2f}% round trip:")
    for apr in (10, 25, 50, 100, 250):
        hours_needed = ROUND_TRIP_PCT / (apr / (365 * 24))
        verdict = "takeable" if hours_needed < median_h else "too slow"
        print(f"    {apr:>4}% APR -> needs {hours_needed:>7.1f}h "
              f"({hours_needed / 24:>5.1f}d) to cover fees   {verdict}")


# --------------------------------------------------------------------------- #
# 2. Funding carry, single venue
# --------------------------------------------------------------------------- #

def section_carry(snaps: dict[str, list[Row]], capital: float, top: int) -> None:
    latest = list(snaps.values())[-1]
    spot_keys = {(r.venue, r.base) for r in latest if r.kind == SPOT}

    hedgeable = [r for r in latest
                 if r.kind == PERP and (r.venue, r.base) in spot_keys and r.apr > 0]
    hedgeable.sort(key=lambda r: r.apr, reverse=True)

    print("\n" + "=" * 78)
    print("2. FUNDING CARRY (latest snapshot) — long spot + short perp, same venue")
    print("=" * 78)
    if not hedgeable:
        print("  no positive-funding perp has a spot market on the same venue")
        return
    print(f"{'VENUE':<12}{'SYMBOL':<18}{'INT':>5}{'APR':>9}{'NET*':>8}"
          f"{'TURNOVER':>11}{'$/YR':>9}")
    print("-" * 78)
    for r in hedgeable[:top]:
        # 4 rotations/yr, buffer 25% of notional -> notional = 0.8 x capital
        net = 0.8 * (r.apr - (SPOT_FEE_PCT * 2 + fee(r.venue) * 2) * 4)
        print(f"{r.venue:<12}{r.symbol:<18}{r.funding_interval_h:>4.0f}h"
              f"{r.apr:>8.1f}%{net:>7.1f}%{r.turnover_musd:>10.0f}M"
              f"{capital * net / 100:>8,.0f}")
    print("  * net on capital, assuming a 25% margin buffer and 4 rotations/year")


# --------------------------------------------------------------------------- #
# 3. Cross-venue dispersion
# --------------------------------------------------------------------------- #

def same_asset(legs: list[Row]) -> list[Row]:
    """Drop legs whose price says they are a different asset wearing the ticker.

    Contract multipliers are always exact powers of ten — 1000PEPE against PEPE
    is 1000x, and those legs ARE the same asset with comparable funding. A price
    ratio that is not a clean power of ten is not a contract size, it is a
    collision: whitebit's CAT_PERP is Caterpillar Inc. at $883 and mexc's
    CAT_USDT is the memecoin at $0.0000014, and pairing them produces a spread
    between a machinery manufacturer and a cat.

    Four of 748 multi-venue bases collide this way today (CAT, HK50, EDGE, BOB).
    The largest cluster wins, so the four real memecoin legs survive and the
    stock is the one dropped.
    """
    priced = [r for r in legs if r.mark > 0]
    if len(priced) < 2:
        return legs
    # Bucket by order of magnitude, after removing the power-of-ten multiplier.
    buckets: dict[int, list[Row]] = defaultdict(list)
    for r in priced:
        buckets[round(math.log10(r.mark))].append(r)
    if len(buckets) == 1:
        return legs
    # Merge buckets that differ by a whole power of ten into their nearest
    # neighbour chain, then keep the largest resulting group.
    keys = sorted(buckets)
    groups, current = [], [keys[0]]
    for prev, k in zip(keys, keys[1:]):
        if k - prev <= 4:          # 1e4 covers every real multiplier in use
            current.append(k)
        else:
            groups.append(current)
            current = [k]
    groups.append(current)
    best = max(groups, key=lambda g: sum(len(buckets[k]) for k in g))
    return [r for k in best for r in buckets[k]]


def section_cross(snaps: dict[str, list[Row]], capital: float, top: int,
                  cycles: int, leg_turnover: float = 5.0,
                  include_baseline: bool = False) -> None:
    """Both legs must be tradeable, which is a stricter test than it sounds.

    Two filters, each of which alone lets a phantom to the top of the table:

    * BOTH legs need real turnover. Screening one leg ranks spreads whose other
      side cannot be entered at size — and load() exempts zero-turnover rows
      from --min-turnover entirely, so they arrive here unscreened.
    * Neither leg may be sitting on the interest-rate floor. A venue quoting
      10.95% because nothing is happening is not the second leg of a trade, and
      three of the top fifteen spreads rested on exactly that.
    """
    latest = list(snaps.values())[-1]
    by_base: dict[str, dict[str, Row]] = defaultdict(dict)
    dropped_thin = dropped_baseline = 0
    for r in latest:
        if r.kind != PERP:
            continue
        if r.turnover_musd < leg_turnover:
            dropped_thin += 1
            continue
        if r.at_baseline and not include_baseline:
            dropped_baseline += 1
            continue
        cur = by_base[r.base].get(r.venue)
        if cur is None or r.turnover_musd > cur.turnover_musd:
            by_base[r.base][r.venue] = r

    spreads = []
    dropped_collision = 0
    for base, all_legs in by_base.items():
        if len(all_legs) < 2:
            continue
        kept = same_asset(list(all_legs.values()))
        dropped_collision += len(all_legs) - len(kept)
        legs = {r.venue: r for r in kept}
        if len(legs) < 2:
            continue
        hi = max(legs.values(), key=lambda r: r.apr)
        lo = min(legs.values(), key=lambda r: r.apr)
        if hi.venue == lo.venue:
            continue
        gross = hi.apr - lo.apr
        fees = (fee(hi.venue) + fee(lo.venue)) * 2 * cycles
        spreads.append((gross - fees, base, lo, hi, gross, fees, len(legs)))
    spreads.sort(reverse=True, key=lambda s: s[0])

    print("\n" + "=" * 78)
    print("3. CROSS-VENUE SPREADS (latest) — long the low leg, short the high leg")
    print("=" * 78)
    print(f"  both legs require >${leg_turnover:g}M turnover"
          + ("" if include_baseline else " and a rate off the 10.95% floor"))
    print(f"  excluded: {dropped_thin:,} thin quotes, "
          f"{dropped_baseline:,} at the floor, "
          f"{dropped_collision} ticker collisions")
    if not spreads:
        print("  nothing survives both filters — loosen --min-leg-turnover")
        return
    print(f"{'ASSET':<10}{'LONG @':<13}{'APR':>8}{'SHORT @':<13}{'APR':>8}"
          f"{'GROSS':>8}{'NET':>8}{'$/YR':>9}")
    print("-" * 78)
    for net, base, lo, hi, gross, _fees, _n in spreads[:top]:
        print(f"{base:<10}{lo.venue:<13}{lo.apr:>7.1f}%{hi.venue:<13}{hi.apr:>7.1f}%"
              f"{gross:>7.1f}%{net:>7.1f}%{capital * net / 100:>8,.0f}")

    contributions: dict[str, int] = defaultdict(int)
    for _net, _b, lo, hi, _g, _f2, _n in spreads[:40]:
        contributions[lo.venue] += 1
        contributions[hi.venue] += 1
    print("\n  Which venues supply the dispersion (appearances in the top 40):")
    for venue, n in sorted(contributions.items(), key=lambda x: -x[1])[:10]:
        print(f"    {venue:<14}{n:>4}")
    print("  Not modelled: basis divergence between the two venues. It is the "
          "dominant\n  risk here and can exceed the whole spread.")


# --------------------------------------------------------------------------- #
# 4. Calendar basis
# --------------------------------------------------------------------------- #

def section_basis(snaps: dict[str, list[Row]], top: int,
                  min_turnover: float = 1.0) -> None:
    latest = list(snaps.values())[-1]
    ts = latest[0].ts if latest else ""
    spot_px: dict[tuple[str, str], float] = {}
    for r in latest:
        if r.kind == SPOT and r.mark > 0:
            spot_px[(r.venue, r.base)] = r.mark
    # Fall back to any venue's spot for the same asset when the future's own
    # venue has no spot market — Deribit being the case that matters.
    any_spot: dict[str, float] = {}
    for (_v, base), px in spot_px.items():
        any_spot.setdefault(base, px)

    rows = []
    thin = 0
    for r in latest:
        if r.kind != FUTURE or r.mark <= 0 or not r.expiry:
            continue
        # A future nobody trades has no basis to measure. 143 of 179 turn over
        # under $1M, and on those the printed premium was the age of the last
        # trade rather than a market view — one contract read 30% purely
        # because two expiries shared a stale print.
        if r.turnover_musd < min_turnover:
            thin += 1
            continue
        px = spot_px.get((r.venue, r.base)) or any_spot.get(r.base) or r.index
        if not px:
            continue
        try:
            days = (datetime.fromisoformat(r.expiry).date()
                    - datetime.fromisoformat(ts[:10]).date()).days
        except ValueError:
            continue
        if days <= 0:
            continue
        basis = (r.mark - px) / px * 100
        rows.append((basis * 365 / days, r, days, basis))
    rows.sort(reverse=True, key=lambda x: x[0])

    print("\n" + "=" * 78)
    print("4. CALENDAR BASIS (latest) — long spot + short dated future, held to expiry")
    print("=" * 78)
    print(f"  futures below ${min_turnover:g}M turnover excluded: {thin}")
    if not rows:
        print("  no dated futures with a usable spot reference in this archive")
        return
    print(f"{'VENUE':<11}{'SYMBOL':<22}{'EXPIRY':<12}{'DAYS':>6}{'BASIS':>8}"
          f"{'ANN':>8}{'NET**':>8}")
    print("-" * 78)
    for ann, r, days, basis in rows[:top]:
        net = (basis - (SPOT_FEE_PCT * 2 + fee(r.venue))) * 365 / days
        print(f"{r.venue:<11}{r.symbol:<22}{r.expiry:<12}{days:>6}"
              f"{basis:>7.2f}%{ann:>7.2f}%{net:>7.2f}%")
    print(f"  ** held to settlement, so the future costs no closing fee. "
          f"Risk-free is {RISK_FREE_PCT}%.")


# --------------------------------------------------------------------------- #
# 5. Paper run
# --------------------------------------------------------------------------- #

def section_paper(snaps: dict[str, list[Row]], capital: float,
                  threshold: float) -> None:
    """Naive rule: hold the single best hedgeable carry, switch when it decays.

    Deliberately naive — it exists to bound the opportunity, not to be a strategy.
    It charges a full round trip on every switch, which is the cost that kills
    real bots, and it ignores slippage, which flatters it.
    """
    stamps = list(snaps)
    if len(stamps) < 3:
        print("\n5. PAPER RUN — need at least 3 snapshots")
        return

    gap = median_gap_hours(stamps)
    max_gap = gap * 2.5

    held: Row | None = None
    pnl_pct = 0.0
    switches = 0
    counted_h = 0.0
    skipped_h = 0.0

    for i in range(1, len(stamps)):
        prev_ts, ts = stamps[i - 1], stamps[i]
        dt_h = hours_between(prev_ts, ts)
        batch = snaps[ts]
        spot_keys = {(r.venue, r.base) for r in batch if r.kind == SPOT}
        cands = [r for r in batch if r.kind == PERP
                 and (r.venue, r.base) in spot_keys and r.apr > threshold]

        if dt_h > max_gap:
            # A scanner outage is not a held position. Crediting funding across
            # it — at the rate observed after the gap, no less — invents returns.
            skipped_h += dt_h
            held = None
            continue

        if held is not None:
            # kind == PERP is load-bearing: on Binance/Bybit/MEXC the spot and
            # perp symbols are the identical string, and matching the spot row
            # gives apr == 0, force-closing and re-entering every single
            # snapshot. Which row won depended on thread completion order, so
            # the same archive produced a different answer on every run.
            live = next((r for r in batch if r.kind == PERP
                         and r.venue == held.venue and r.symbol == held.symbol), None)
            if live is None or live.apr < threshold:
                held = None
            else:
                # Accrue at the rate observed at the START of the interval —
                # using the end rate is look-ahead bias.
                pnl_pct += held.apr / 100 / 365 / 24 * dt_h * 100
                counted_h += dt_h
                held = live

        if held is None and cands:
            best = max(cands, key=lambda r: r.apr)
            pnl_pct -= SPOT_FEE_PCT * 2 + fee(best.venue) * 2
            switches += 1
            held = best

    span_h = hours_between(stamps[0], stamps[-1]) - skipped_h
    annualised = pnl_pct * (365 * 24 / span_h) if span_h > 0 else 0.0

    print("\n" + "=" * 78)
    print("5. PAPER RUN — always hold the single best hedgeable carry")
    print("=" * 78)
    print(f"  window            {span_h:.1f}h usable"
          + (f"  ({skipped_h:.1f}h skipped as data gaps)" if skipped_h else ""))
    print(f"  entries           {switches}")
    print(f"  time in position  {counted_h:.1f}h "
          f"({counted_h / span_h * 100 if span_h else 0:.0f}%)")
    print(f"  realised          {pnl_pct:+.3f}% on notional")
    print(f"  annualised        {annualised:+.1f}%   (on ${capital:,.0f}: "
          f"${capital * annualised / 100:+,.0f}/yr)")
    print(f"  vs risk-free      {annualised - RISK_FREE_PCT:+.1f}%")
    if span_h < 168:
        print("\n  Too short to mean anything. This number becomes informative at "
              "~4 weeks;\n  before that it mostly measures which day you started on.")


# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--capital", type=float, default=10_000)
    p.add_argument("--min-turnover", type=float, default=1.0,
                   help="ignore perps below this 24h turnover in $M (default: 1)")
    p.add_argument("--drop-unknown-turnover", action="store_true",
                   help="also exclude perps whose venue reported no turnover; "
                        "they are kept by default but are NOT screened by "
                        "--min-turnover")
    p.add_argument("--threshold", type=float, default=15.0,
                   help="APR%% that counts as an opportunity (default: 15)")
    p.add_argument("--cycles", type=int, default=12,
                   help="assumed cross-venue round trips per year (default: 12)")
    p.add_argument("--min-leg-turnover", type=float, default=5.0,
                   help="cross-venue: BOTH legs must exceed this 24h turnover "
                        "in $M (default: 5)")
    p.add_argument("--include-baseline", action="store_true",
                   help="cross-venue: keep quotes sitting on the 10.95%% "
                        "interest-rate floor; excluded by default because they "
                        "mean the venue is idle, not that a spread exists")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--section", default="all",
                   choices=["all", "coverage", "persistence", "carry",
                            "cross", "basis", "paper"],
                   help="which section to print (default: all)")
    args = p.parse_args()

    rows = load(args.data_dir, args.min_turnover, args.drop_unknown_turnover)
    snaps = by_timestamp(rows)
    if not snaps:
        print("No rows survived filtering — every section below would be empty.\n"
              "Lower --min-turnover, or drop --drop-unknown-turnover.",
              file=sys.stderr)
        sys.exit(1)
    want = args.section

    if want in ("all", "coverage"):
        section_coverage(rows, snaps)
    if want in ("all", "persistence"):
        section_persistence(snaps, args.threshold)
    if want in ("all", "carry"):
        section_carry(snaps, args.capital, args.top)
    if want in ("all", "cross"):
        section_cross(snaps, args.capital, args.top, args.cycles,
                      args.min_leg_turnover, args.include_baseline)
    if want in ("all", "basis"):
        section_basis(snaps, args.top, args.min_turnover)
    if want in ("all", "paper"):
        section_paper(snaps, args.capital, args.threshold)


if __name__ == "__main__":
    main()
