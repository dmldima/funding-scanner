#!/usr/bin/env python3
"""Collect one snapshot from every venue and append it to the daily archive.

Writes gzipped CSV partitioned by UTC date: data/YYYY-MM-DD.csv.gz. Partitioning
by day keeps each git commit small, keeps any single file readable with one
`zcat`, and means a corrupt write can only ever cost one day.

This script makes NO trading decisions and computes no strategy. It fetches and
stores. All interpretation lives in analyze.py, so the archive stays valid even
when the analysis changes its mind.

Usage:
    python3 scan.py                          # all venues -> data/
    python3 scan.py --venues bybit,gate -v   # subset, verbose
    python3 scan.py --kinds perp             # perps only
    python3 scan.py --dry-run                # fetch and report, write nothing
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import venues
from venues import ADAPTERS, ALL_VENUES, FIELDS, Observation

# Rows below this are noise: a pair nobody trades cannot be entered or exited,
# and storing it forever costs more than it will ever be worth.
MIN_TURNOVER_MUSD = 0.20


def collect(selected: list[str], kinds: list[str], workers: int,
            verbose: bool) -> tuple[list[Observation], dict[str, str]]:
    """Fetch every (venue, kind) pair concurrently. One failure never stops the rest."""
    # One adapter can serve several kinds (Deribit returns perps and dated
    # futures from a single call), so jobs are keyed by the FUNCTION, not by
    # (venue, kind) — otherwise it would be fetched once per kind and every row
    # duplicated. Timing is taken inside the callable: measuring after
    # as_completed yields would record 0.0s for every source.
    jobs: dict[object, tuple[str, list[str]]] = {}
    for venue in selected:
        for kind, fn in ADAPTERS.get(venue, {}).items():
            if kind not in kinds:
                continue
            jobs.setdefault(fn, (venue, []))[1].append(kind)

    def timed(fn):
        def run():
            started = time.time()
            return fn(), time.time() - started
        return run

    observations: list[Observation] = []
    status: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(timed(fn)): meta for fn, meta in jobs.items()}
        for future in as_completed(futures):
            venue, served = futures[future]
            key = f"{venue}:{'+'.join(sorted(served))}"
            try:
                rows, elapsed = future.result()
            except BaseException as exc:   # one venue must never stop the scan
                if isinstance(exc, KeyboardInterrupt):
                    raise
                status[key] = f"FAIL {str(exc) or type(exc).__name__}"[:110]
                if verbose:
                    print(f"  {key}: {status[key]}", file=sys.stderr)
                continue
            rows = [r for r in rows if r.kind in kinds]
            observations.extend(rows)
            status[key] = f"ok {len(rows)} ({elapsed:.1f}s)"
            if verbose:
                print(f"  {key}: {status[key]}", file=sys.stderr)

    # Belt and braces: a duplicate (venue, kind, symbol) would double-count in
    # every later aggregation and is invisible once archived.
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for o in observations:
        key3 = (o.venue, o.kind, o.symbol)
        if key3 in seen:
            continue
        seen.add(key3)
        unique.append(o)
    return unique, status


def keep(obs: Observation) -> bool:
    """Drop dust, keep anything that could matter to any strategy later.

    A perp with a non-zero funding rate is kept even without reported turnover:
    several venues simply do not publish volume, and discarding them would
    silently bias the archive against exactly the small venues we are here for.
    """
    if obs.turnover_musd >= MIN_TURNOVER_MUSD:
        return True
    if obs.kind == venues.PERP and obs.funding_rate != 0.0:
        return True
    return bool(obs.kind == venues.FUTURE and obs.mark > 0)


def append(observations: list[Observation], data_dir: Path, ts: str) -> Path:
    """Append to today's partition, writing the header only on creation.

    Written via a temp file and os.replace so an interrupted run can never leave
    a half-written gzip member behind — a truncated .gz is unreadable end to end,
    not just at the tail, so a partial append would cost the whole day.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{ts[:10]}.csv.gz"

    existing = b""
    if path.exists():
        existing = path.read_bytes()

    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as raw:
            raw.write(existing)
            with gzip.open(raw, "wt", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if not existing:
                    writer.writerow(FIELDS)
                for obs in observations:
                    writer.writerow(obs.row(ts))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def log_status(status: dict[str, str], data_dir: Path, ts: str) -> Path:
    """Append per-source reachability to a permanent log.

    This is not diagnostics — it is data. Several CEXs geo-block by IP, and which
    ones answer from a given runner is exactly the thing you need to know before
    trusting a coverage gap as a market fact rather than a network fact. Written
    even when the scan collected nothing, because that run is the informative one.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "venue_status.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "source", "ok", "detail"])
        for source, detail in sorted(status.items()):
            w.writerow([ts, source, int(detail.startswith("ok")), detail])
    return path


def summarise(observations: list[Observation], status: dict[str, str]) -> None:
    ok = sorted(k for k, v in status.items() if v.startswith("ok"))
    bad = {k: v for k, v in status.items() if not v.startswith("ok")}

    by_kind: dict[str, int] = {}
    by_venue: dict[str, int] = {}
    for o in observations:
        by_kind[o.kind] = by_kind.get(o.kind, 0) + 1
        by_venue[o.venue] = by_venue.get(o.venue, 0) + 1

    print(f"\nSources ok: {len(ok)}/{len(status)}")
    print("  " + ", ".join(ok) if ok else "  none")
    if bad:
        print("Failed:")
        for k, v in sorted(bad.items()):
            print(f"  ! {k}: {v}")

    print(f"\nRows kept: {len(observations)}  "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_kind.items()))})")
    print("Per venue: " + ", ".join(f"{v}={n}" for v, n in
                                    sorted(by_venue.items(), key=lambda x: -x[1])))

    perps = [o for o in observations if o.kind == venues.PERP and o.turnover_musd > 1]
    if perps:
        perps.sort(key=lambda o: o.funding_apr, reverse=True)
        edge = min(3, len(perps) // 2)   # below 6 perps the head and tail overlap
        print("\nWidest funding right now (context only — no strategy applied):")
        for o in perps[:edge] + perps[-edge:] if edge else perps:
            print(f"  {o.venue:<12}{o.symbol:<18}{o.funding_apr:>9.1f}% APR "
                  f"({o.funding_interval_h:g}h)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--venues", default="all", help="comma-separated, or 'all'")
    p.add_argument("--kinds", default="perp,spot,future",
                   help="comma-separated: perp,spot,future")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--dry-run", action="store_true", help="fetch but write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    selected = (ALL_VENUES if args.venues == "all"
                else [v.strip().lower() for v in args.venues.split(",") if v.strip()])
    unknown = [v for v in selected if v not in ADAPTERS]
    if unknown:
        print(f"Unknown venues: {', '.join(unknown)}", file=sys.stderr)
        sys.exit(2)
    kinds = [k.strip().lower() for k in args.kinds.split(",") if k.strip()]

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Scanning {len(selected)} venues at {ts}", file=sys.stderr)

    raw, status = collect(selected, kinds, args.workers, args.verbose)
    observations = [o for o in raw if keep(o)]
    summarise(observations, status)

    # The status log is written first and unconditionally: a run where every
    # venue was blocked is precisely the run whose record matters most.
    if not args.dry_run:
        log_status(status, args.data_dir, ts)

    if not observations:
        print("\nNo usable rows collected — status logged, nothing archived.",
              file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    path = append(observations, args.data_dir, ts)
    print(f"\nAppended {len(observations)} rows to {path} "
          f"({path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
