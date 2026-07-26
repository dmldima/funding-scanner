# Audit — funding-scanner

**Date:** 2026-07-27
**Scope:** whole project — `venues.py`, `scan.py`, `analyze.py`, `.github/workflows/scan.yml`
**Base:** commit `4d7a6ab`
**Method:** all findings verified by running the code against the live archive
(`data/2026-07-26.csv.gz`, 5,950 rows, 1 snapshot) and against live venue APIs.
Nothing below is inferred from reading alone.

## Structure

| File | Lines | Role |
|---|---:|---|
| `venues.py` | 729 | 16 venue adapters, HTTP, symbol normalisation, data model |
| `analyze.py` | 524 | 6 report sections over the archive |
| `scan.py` | 239 | orchestration, filtering, atomic append, status log |
| `.github/workflows/scan.yml` | 30 | 30-min schedule, commit to `data/` |

No dependencies beyond the standard library. No tests, no CI beyond the scan job.

---

## Critical

### 1. `base_asset()` cannot parse OKX linear futures — 61% of all futures rows are silently dropped from the calendar-basis section

`venues.py:118` · `analyze.py:370`

OKX names its linear-settled dated futures `BTC-USD_UM-260925` and its long-dated
ones `NVDA-USD_UM_XPERP-310613`. `base_asset()` strips the expiry, removes
separators, then looks for a quote suffix — but the string now ends in `UM`, not
`USD`, so nothing is stripped:

```
BTC-USD_UM-260925        -> BTCUSDUM      (expected BTC)
NVDA-USD_UM_XPERP-310613 -> NVDAUSDUMX    (expected NVDA)
```

`section_basis` then looks up a spot price by `(venue, base)`, falls back to any
venue's spot for that base, and finally to `r.index` — but `okx_future()`
(`venues.py:377`) never populates `index`. All three lookups miss, and
`analyze.py:371` does `continue`. No warning is printed.

**Measured on the live archive:**

| | rows |
|---|---:|
| futures collected | 179 |
| usable in section 4 | 70 |
| **silently dropped** | **109 (all OKX)** |

OKX contributes 125 of 179 futures rows and 109 of them vanish. With Binance and
Bybit geo-blocked on the runner, OKX is the largest dated-futures source in the
archive, and section 4 currently reports on `gate` (30) + `deribit` (24) +
16 stragglers from OKX's inverse contracts, while presenting itself as complete.

**Fix:** treat `_UM` / `_UM_XPERP` as kind markers (same list as `PERP`/`SWAP`),
or strip `USD` before the marker. Roughly a two-line change to `_KIND_SUFFIX`
and the quote loop. **Also populate `index` in `okx_future()`** — the ticker
call it already makes carries a usable reference price, which would have made
this failure impossible to hide.

### 2. `--min-turnover` is inert for the venues that dominate the output

`analyze.py:101` · `scan.py:103`

The filter is `if kind == PERP and turnover > 0 and turnover < min_turnover`. A
row reporting **zero** turnover is never filtered, by design — the comment argues
that 0 means "venue publishes no volume", not "nobody trades it".

The consequence is not what the comment anticipates. Measured:

| venue | perp rows with turnover == 0 |
|---|---|
| **bingx** | **792 / 792 (100%)** |
| paradex | 45 / 83 |
| dydx | 17 / 28 |
| bitmex | 5 / 44 |

Running the cross-venue section with `--min-turnover 999999999` — a threshold no
real instrument could clear — still prints a full table of 40 spreads, and its
"which venues supply the dispersion" verdict is:

```
bingx     37
paradex   31
dydx      10
coinex     1
htx        1
```

Every headline venue is a zero-turnover reporter. That answer is a measurement
artifact, not a market fact — precisely the confusion the README's philosophy is
built to prevent. A user who sets `--min-turnover 50` to screen for liquidity
gets a table led by instruments whose liquidity is unknown.

Note `bingx_perp()` (`venues.py:580`) sets no `turnover_musd` **at all**, so
BingX is not a venue that declines to publish volume — the adapter simply never
reads it. BingX does expose 24h quote volume on a separate ticker endpoint.

**Fix:** two parts. (a) Populate BingX turnover from
`/openApi/swap/v2/quote/ticker`. (b) Make the unknown-liquidity case *visible*
rather than silently included — either a separate `--allow-unknown-turnover`
flag, or mark such rows in the output with a symbol and print a count. Keeping
them is defensible; presenting them as if screened is not.

### 3. The workflow discards `venue_status.csv` on exactly the run where it matters most

`.github/workflows/scan.yml:26` · `scan.py:219-227`

`scan.py` is explicit that a total-blackout run is the informative one:

> *"The status log is written first and unconditionally: a run where every venue
> was blocked is precisely the run whose record matters most."*

It writes `venue_status.csv`, then calls `sys.exit(1)` when no rows were
collected. In the workflow, that fails the `Run scan` step, and `Commit results`
has no `if:` condition — so it defaults to `success()` and is **skipped**. The
status file is written to the runner's disk and thrown away with the runner.

The README states `venue_status.csv` is *"written on EVERY run"*. It is written
on every run and **committed on every run except the blackout ones**. If the
scanner is geo-blocked wholesale for a week, the archive shows a gap with no
record of why — the exact ambiguity the file exists to resolve.

**Fix:** one line — `if: always()` on the `Commit results` step. Cost: trivial.

---

## Important

### 4. OKX perp coverage is capped at 150 of 411 instruments, and the truncation is not recorded

`venues.py:350`

`ranked[:150]` keeps the liquid head and drops the rest. Measured against the
live API: **411 linear swaps listed, 150 fetched, 261 dropped.**

The adapter carefully reports the *other* kind of loss — `skipped` counts
instruments whose funding call failed and prints a line (`venues.py:369-373`) —
but the 150-cap truncation is invisible. Nothing in the archive distinguishes
"OKX lists 150 perps" from "we sampled 150 of 411", so a future coverage analysis
will draw the wrong conclusion, and the marginal-liquidity tail this project
exists to capture is exactly what gets cut.

The cap is defensible: funding is a per-instrument call on OKX, and the observed
cost is already **45.2s for one venue** against 0.3–3.1s for every other. The
problem is the silence, not the limit.

**Fix:** log the truncation the same way `skipped` is logged, and make the cap a
CLI argument. Ten lines.

### 5. `section_cross` / `section_basis` / `section_paper` crash on an empty archive; `section_carry` is guarded three times

`analyze.py:263-272`, `303`, `354`

```python
def section_carry(...):
    if not snaps:                      # line 263
        print('\n  (no snapshots after filtering)')
        return
    if not snaps:                      # line 266 — identical
        ...
    if not snaps:                      # line 269 — identical
        ...
    latest = list(snaps.values())[-1]
```

The same guard is pasted three times in one function, while `section_cross:303`,
`section_basis:354` and `section_paper` have none — `list({}.values())[-1]`
raises `IndexError`. Reachable whenever a filter empties the set, e.g. a
`--min-turnover` above every instrument on an archive with no zero-turnover rows,
or a `data/` holding only failed-scan partitions.

The triple paste is worth reading as a signal: it suggests the guard was applied
by search-and-replace that landed three times in one place and zero times in the
other three.

**Fix:** one guard in `main()` before dispatch, delete all three. Five lines.

### 6. README claims funding intervals are never assumed; five venues assume 8h

`README.md:25` vs `venues.py`

> *"The funding **interval** is read per instrument, never assumed."*

Verified across all 16 adapters:

| Behaviour | Venues |
|---|---|
| read per instrument | binance, bybit, gate, kucoin, bingx, okx (derived), coinex (derived) |
| genuinely fixed hourly | hyperliquid, dydx, backpack |
| **hardcoded 8.0** | **bitget, mexc, htx, bitmex, paradex, deribit** |

I checked whether the data is available and ignored — as it was in the HTX mark
bug. It is not: MEXC's `contract/ticker` and `contract/detail`, Bitget's
`mix/market/tickers` and Paradex's `markets/summary` carry no interval field, so
the hardcode is a reasonable choice given one batch call per venue. MEXC is the
material risk — it moves symbols to 4h and 1h, and the README's own argument
says an 8h assumption understates those by 2–8×.

**Fix:** the code is fine; the README sentence is false as written. Correct it to
name which venues are read and which are assumed, so the archive's limits are
visible to whoever reads the numbers later. Ten minutes.

### 7. No tests, and no CI that runs anything but the scanner

There is no test suite. For a project whose entire value is the *correctness of a
growing archive that cannot be recomputed*, the untested surface is
`base_asset()` — a pure function, 40 lines, with two documented near-miss traps
in its own docstring, and finding #1 above is a third one it does not handle.

`base_asset()` is the cheapest possible thing to test: pure, no I/O, and the
docstring already contains the test cases. Twenty parametrised assertions would
have caught #1 before the first archived row.

**Fix:** `tests/test_base_asset.py` with the docstring cases plus the OKX shapes,
and a second workflow running `pytest` on push. Under an hour, and it is the
highest value-per-minute change in the repository.

---

## Worth knowing

| Finding | Location | Note |
|---|---|---|
| Retry sleeps after the final attempt | `venues.py:68` | The `time.sleep` is inside the loop with no last-iteration check, so every hard failure pays an extra 3.2s before raising. With 16 venues in an 8-worker pool this adds real wall-clock on a blocked runner. Two lines. |
| `deribit_all` hardcodes `base=currency` | `venues.py:686` | Correct today (only BTC/ETH are fetched) but bypasses `base_asset()`, so it will silently mis-key if a third currency is added. |
| Fee table is a second source of truth | `analyze.py:42-47` | 16 venue fees with no date and no source comment. They drift, and every net figure in three sections depends on them. Add the as-of date. |
| `RISK_FREE_PCT = 3.81` hardcoded | `analyze.py:50` | Comment says refresh manually. It will not be refreshed. Low harm — it appears in one line of output. |

---

## What is genuinely well built

Worth naming precisely, because it tells you where *not* to spend attention:

- **Error isolation in `collect()`** (`scan.py:70`) is correct in the way most
  batch loops are not: it catches `BaseException`, re-raises `KeyboardInterrupt`
  explicitly, records the failure as data, and continues. One venue failing
  genuinely cannot stop a scan.
- **The atomic append** (`scan.py:110-137`) reasons correctly about gzip: a
  truncated `.gz` is unreadable end-to-end, not just at the tail, so a partial
  append costs the whole day. Temp file plus `os.replace` is the right answer,
  and the `finally: unlink` cleans up on the error path.
- **Jobs keyed by function, not by `(venue, kind)`** (`scan.py:47-52`) — this is
  the non-obvious correct choice, and the comment explains why: Deribit serves
  two kinds from one call, and keying by kind would duplicate every row.
- **The gap-breaks-the-run logic** in `section_persistence` (`analyze.py:205`)
  and the outage handling in `section_paper` (`analyze.py:435-440`). Crediting
  funding across a scanner outage would manufacture exactly the headline the
  project exists to produce, and both sections refuse to. The paper run also
  accrues at the *start*-of-interval rate to avoid look-ahead bias
  (`analyze.py:453-455`).
- **The `kind == PERP` guard in the paper run** (`analyze.py:448`) with its
  comment about thread-completion order producing different answers on every run.
  That is a real bug that was found and fixed, and the comment preserves why.
- **Adapter comments record past corruption, not intent.** The BitMEX satoshi
  bug, the KuCoin contract multiplier, the `kPEPE`-before-`.upper()` trap, the
  dYdX `FINAL_SETTLEMENT` markets. These are the notes of someone who has
  already been burned and wrote it down.

The defects above are concentrated in one place: **symbol normalisation and
liquidity filtering** — the two functions where a silent miss looks identical to
"the venue doesn't list it". Everything around them is careful.

---

## Ranked summary

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | OKX futures dropped from calendar basis (109/179 rows) | Critical | **fixed** — `_SETTLE_MARKER`, `venues.py:116` |
| 2 | `--min-turnover` inert; BingX turnover never collected | Critical | **fixed** — `bingx_perp()` + `--drop-unknown-turnover` |
| 3 | `venue_status.csv` not committed on blackout runs | Critical | **fixed** — `if: always()` |
| 7 | No tests on `base_asset()`, no test CI | Important | **fixed** — `tests/`, 11 cases, `tests.yml` |
| 4 | OKX 150/411 cap unrecorded | Important | open — ~10 lines |
| 5 | Missing empty-snapshot guards; triple-pasted guard | Important | **fixed** — single guard in `main()` |
| 6 | README contradicts code on funding intervals | Important | open — doc edit |

### Verified after the fixes

| | before | after |
|---|---:|---:|
| BingX perp rows with no turnover | 792 / 792 | **19 / 792** |
| OKX futures with an unresolvable base | 109 / 125 | **0 / 130** |
| `base_asset()` test cases | 0 | **11** |

Remaining: #4 (silent 150-of-411 OKX cap) and #6 (README overstates that funding
intervals are never assumed). Neither corrupts stored data — #4 bounds coverage
and #6 is a documentation claim — so both can wait for the next pass.

Note the archive is append-only: the 109 OKX rows already written with broken
bases stay broken. At one snapshot of history that is immaterial, but it is the
reason a normalisation bug is worth catching early rather than living with.
