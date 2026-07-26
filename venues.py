#!/usr/bin/env python3
"""Venue adapters: one uniform Observation per instrument, across many exchanges.

Deliberately collects MORE than any single strategy needs, because the point of
the archive is that the strategy question stays open. With perps, spot and dated
futures all captured on the same timestamp, the stored data can later answer:

  * perp funding carry        — perp funding + spot price, same venue
  * cross-venue funding       — perp funding on venue A vs venue B
  * calendar basis            — dated future price vs spot price
  * perp vs dated future      — funding stream vs calendar premium, same venue
  * cross-venue calendar      — dated future on A vs dated future on B
  * reverse carry feasibility — whether a spot market exists at all to borrow

None of those decisions are made here. This module only fetches and normalises.
Every adapter is isolated: one venue failing never stops a scan.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = "funding-archive/1.0"
TIMEOUT = 25.0

PERP, SPOT, FUTURE = "perp", "spot", "future"


class VenueError(RuntimeError):
    """A venue's API did not return usable data."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _request(url: str, *, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 451 is the one that matters on CI: several CEXs geo-block US IPs,
            # and GitHub-hosted runners are US-based. Fail fast, do not retry.
            if exc.code == 451:
                raise VenueError(f"HTTP 451 geo-blocked: {url}") from exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise VenueError(f"HTTP {exc.code}: {url}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(0.8 * 2 ** attempt + random.uniform(0, 0.3))
    raise VenueError(f"failed after 3 attempts: {url}: {last}")


def _get(url: str, params: dict | None = None) -> object:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url)


def _f(value: object, default: float = 0.0) -> float:
    """Venues return numbers as strings, nulls, empty strings, or omit them."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ms_to_iso(value: object) -> str:
    ms = _f(value)
    if ms <= 0:
        return ""
    if ms > 1e13:                 # microseconds
        ms /= 1000
    elif ms < 1e11:               # seconds — a unit slip here would silently
        ms *= 1000                # yield 1970 dates rather than fail

    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
            timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return ""


# --------------------------------------------------------------------------- #
# Symbol normalisation
# --------------------------------------------------------------------------- #

# Longest first — KuCoin uses XBTUSDTM, so USDTM must be tried before USDT.
_QUOTES = ("USDTM", "USDCM", "USDM", "USDT", "USDC", "USD")
_KIND_SUFFIX = ("PERPETUAL", "PERP", "SWAP")
# Only real contract multipliers, never a bare \d+: L3, G3, M87 and X2Y2 are
# ticker names, and a greedy \d+$ turns them into L, G, M and X2Y.
_MULT_PREFIX = re.compile(r"^(1000000|100000|10000|1000)(?=[A-Z])")
_MULT_SUFFIX = re.compile(r"(1000000|100000|10000|1000)$")
_EXPIRY_SUFFIX = re.compile(r"[-_]?(\d{6}|\d{8}|\d{1,2}[A-Z]{3}\d{2})$")
# OKX marks its linear-settled dated futures _UM and its long-dated
# perpetual-style contracts _UM_XPERP: BTC-USD_UM-260925, NVDA-USD_UM_XPERP-310613.
# Stripped here, while the delimiter is still present, and deliberately NOT via
# _KIND_SUFFIX: that runs after separators are removed, so a bare "UM" there
# would also eat the tail of any ticker legitimately ending in those letters.
_SETTLE_MARKER = re.compile(r"[-_](UM|XPERP)(?=[-_]|$)")


def base_asset(raw: str) -> str:
    """Reduce a venue-specific symbol to a comparable base asset.

    BTCUSDT / BTC-USDT-SWAP / XBTUSDTM / BTC-PERP / BTCUSDT_261225 -> BTC
    BTC-USD_UM-260925 / NVDA-USD_UM_XPERP-310613                   -> BTC / NVDA
    1000PEPEUSDT / PEPE1000USDT / kPEPE-USD                        -> PEPE

    Multiplier prefixes are stripped because 1000PEPE and PEPE are the same
    asset with different contract sizes — their funding rates are directly
    comparable, and not stripping them silently breaks cross-venue pairing.

    Two traps this deliberately avoids, both of which corrupt the archive in a
    way no later analysis can undo:

    * The lowercase-k multiplier (kPEPE) must be removed BEFORE uppercasing,
      or it is indistinguishable from a real leading K and KAVA becomes AVA —
      which then collides with the genuine AVA token on another venue and the
      cross-venue section prints a spread between two unrelated assets.
    * The quote suffix is stripped exactly ONCE. Looping until stable turns
      TUSDUSDT into T (colliding with Threshold), BUSD into B, PYUSD into PY.
    """
    s = raw.strip()
    if s[:1] == "k" and s[1:2].isupper():
        s = s[1:]                        # kPEPE -> PEPE, before .upper()
    s = s.upper()
    s = _EXPIRY_SUFFIX.sub("", s)
    s = _SETTLE_MARKER.sub("", s)     # BTC-USD_UM -> BTC-USD, before separators go
    s = s.replace("XBT", "BTC")
    for sep in ("-", "_", "/", ":"):
        s = s.replace(sep, "")
    changed = True
    while changed:                       # markers stack: BTCUSDTSWAP, SOLUSDCPERP
        changed = False
        for k in _KIND_SUFFIX:
            if s.endswith(k) and len(s) > len(k):
                s = s[: -len(k)]
                changed = True
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            s = s[: -len(q)]
            break                        # once, not until stable
    s = _MULT_PREFIX.sub("", s)
    s = _MULT_SUFFIX.sub("", s)
    return s or raw.upper()


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

FIELDS = ["ts", "venue", "kind", "symbol", "base", "expiry", "mark", "index",
          "funding_rate", "funding_interval_h", "next_funding",
          "turnover_musd", "oi_musd"]


@dataclass(frozen=True)
class Observation:
    venue: str
    kind: str                     # perp | spot | future
    symbol: str
    base: str
    mark: float = 0.0
    index: float = 0.0            # spot/index reference where the venue gives one
    funding_rate: float = 0.0     # per interval, fraction; perps only
    funding_interval_h: float = 0.0
    next_funding: str = ""
    expiry: str = ""              # ISO date; futures only
    turnover_musd: float = 0.0
    oi_musd: float = 0.0
    ts: str = field(default="")

    @property
    def funding_apr(self) -> float:
        """Percent per year on notional. The interval is what makes venues
        comparable: 1%/1h on Hyperliquid is eight times 1%/8h on Bybit."""
        if self.kind != PERP or self.funding_interval_h <= 0:
            return 0.0
        return self.funding_rate * (24 / self.funding_interval_h) * 365 * 100

    def row(self, ts: str) -> list:
        return [ts, self.venue, self.kind, self.symbol, self.base, self.expiry,
                f"{self.mark:.10g}", f"{self.index:.10g}",
                f"{self.funding_rate:.10g}", f"{self.funding_interval_h:g}",
                self.next_funding, f"{self.turnover_musd:.4f}",
                f"{self.oi_musd:.4f}"]


# --------------------------------------------------------------------------- #
# Binance
# --------------------------------------------------------------------------- #

def binance_perp() -> list[Observation]:
    prem = {p["symbol"]: p for p in _get("https://fapi.binance.com/fapi/v1/premiumIndex")}
    tick = {t["symbol"]: t for t in _get("https://fapi.binance.com/fapi/v1/ticker/24hr")}
    intervals: dict[str, float] = {}
    try:
        for row in _get("https://fapi.binance.com/fapi/v1/fundingInfo"):
            intervals[row["symbol"]] = _f(row.get("fundingIntervalHours"), 8.0)
    except VenueError:
        pass
    info = {s["symbol"]: s for s in
            _get("https://fapi.binance.com/fapi/v1/exchangeInfo")["symbols"]}

    out = []
    for sym, p in prem.items():
        if info.get(sym, {}).get("contractType") != "PERPETUAL":
            continue
        out.append(Observation(
            "binance", PERP, sym, base_asset(sym),
            mark=_f(p.get("markPrice")), index=_f(p.get("indexPrice")),
            funding_rate=_f(p.get("lastFundingRate")),
            funding_interval_h=intervals.get(sym, 8.0),
            next_funding=_ms_to_iso(p.get("nextFundingTime")),
            turnover_musd=_f(tick.get(sym, {}).get("quoteVolume")) / 1e6))
    return out


def binance_future() -> list[Observation]:
    """Dated quarterly futures — the calendar-basis leg."""
    prem = {p["symbol"]: p for p in _get("https://fapi.binance.com/fapi/v1/premiumIndex")}
    tick = {t["symbol"]: t for t in _get("https://fapi.binance.com/fapi/v1/ticker/24hr")}
    out = []
    for s in _get("https://fapi.binance.com/fapi/v1/exchangeInfo")["symbols"]:
        if s.get("contractType") in ("PERPETUAL", "", None):
            continue
        sym = s["symbol"]
        out.append(Observation(
            "binance", FUTURE, sym, base_asset(sym),
            mark=_f(prem.get(sym, {}).get("markPrice")),
            index=_f(prem.get(sym, {}).get("indexPrice")),
            expiry=_ms_to_iso(s.get("deliveryDate"))[:10],
            turnover_musd=_f(tick.get(sym, {}).get("quoteVolume")) / 1e6))
    return out


def binance_spot() -> list[Observation]:
    rows = _get("https://api.binance.com/api/v3/ticker/24hr")
    return [Observation("binance", SPOT, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("lastPrice")),
                        turnover_musd=_f(r.get("quoteVolume")) / 1e6)
            for r in rows if str(r.get("symbol", "")).endswith(("USDT", "USDC"))]


# --------------------------------------------------------------------------- #
# Bybit
# --------------------------------------------------------------------------- #

def _bybit_instruments(category: str) -> list[dict]:
    out, cursor, seen = [], None, set()
    for _page in range(20):          # cap: a cursor that stops advancing would loop forever
        p = {"category": category, "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        res = _get("https://api.bybit.com/v5/market/instruments-info", p)["result"]
        out.extend(res.get("list", []))
        cursor = res.get("nextPageCursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return out


def bybit_perp() -> list[Observation]:
    tick = _get("https://api.bybit.com/v5/market/tickers",
                {"category": "linear"})["result"]["list"]
    meta = {i["symbol"]: i for i in _bybit_instruments("linear")}
    out = []
    for t in tick:
        sym = t["symbol"]
        m = meta.get(sym, {})
        if m.get("contractType") and "Perpetual" not in m["contractType"]:
            continue
        out.append(Observation(
            "bybit", PERP, sym, base_asset(sym),
            mark=_f(t.get("markPrice")), index=_f(t.get("indexPrice")),
            funding_rate=_f(t.get("fundingRate")),
            funding_interval_h=_f(m.get("fundingInterval"), 480) / 60,
            next_funding=_ms_to_iso(t.get("nextFundingTime")),
            turnover_musd=_f(t.get("turnover24h")) / 1e6,
            oi_musd=_f(t.get("openInterestValue")) / 1e6))
    return out


def bybit_future() -> list[Observation]:
    tick = {t["symbol"]: t for t in
            _get("https://api.bybit.com/v5/market/tickers",
                 {"category": "linear"})["result"]["list"]}
    out = []
    for m in _bybit_instruments("linear"):
        if "Futures" not in (m.get("contractType") or ""):
            continue
        sym = m["symbol"]
        t = tick.get(sym, {})
        out.append(Observation(
            "bybit", FUTURE, sym, base_asset(sym),
            mark=_f(t.get("markPrice")), index=_f(t.get("indexPrice")),
            expiry=_ms_to_iso(m.get("deliveryTime"))[:10],
            turnover_musd=_f(t.get("turnover24h")) / 1e6))
    return out


def bybit_spot() -> list[Observation]:
    rows = _get("https://api.bybit.com/v5/market/tickers",
                {"category": "spot"})["result"]["list"]
    return [Observation("bybit", SPOT, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("lastPrice")),
                        turnover_musd=_f(r.get("turnover24h")) / 1e6)
            for r in rows]


# --------------------------------------------------------------------------- #
# OKX
# --------------------------------------------------------------------------- #

def _okx_tickers(inst_type: str) -> dict[str, dict]:
    return {t["instId"]: t for t in
            _get("https://www.okx.com/api/v5/market/tickers",
                 {"instType": inst_type})["data"]}


def okx_perp() -> list[Observation]:
    tick = _okx_tickers("SWAP")
    insts = _get("https://www.okx.com/api/v5/public/instruments",
                 {"instType": "SWAP"})["data"]
    # Funding is per-instrument on OKX, so only the liquid head is queried.
    # Filter BEFORE ranking: inverse swaps rank near the top by volume, and
    # discarding them inside the loop would spend slots on rows that are then
    # thrown away — silently starving the marginal-liquidity tail this archive
    # exists to capture.
    linear = [i for i in insts if i.get("settleCcy") in ("USDT", "USDC")]
    ranked = sorted(linear, key=lambda i: -_f(
        tick.get(i["instId"], {}).get("volCcy24h")) * _f(
        tick.get(i["instId"], {}).get("last")))
    out, skipped = [], 0
    for i in ranked[:150]:
        inst = i["instId"]
        try:
            d = _get("https://www.okx.com/api/v5/public/funding-rate",
                     {"instId": inst})["data"][0]
        except (VenueError, IndexError, KeyError):
            skipped += 1
            continue
        interval = 8.0
        gap = (_f(d.get("nextFundingTime")) - _f(d.get("fundingTime"))) / 3_600_000
        if 0.5 <= gap <= 24:
            interval = gap
        t = tick.get(inst, {})
        out.append(Observation(
            "okx", PERP, inst, base_asset(inst), mark=_f(t.get("last")),
            funding_rate=_f(d.get("fundingRate")), funding_interval_h=interval,
            next_funding=_ms_to_iso(d.get("nextFundingTime")),
            turnover_musd=_f(t.get("volCcy24h")) * _f(t.get("last")) / 1e6))
        time.sleep(0.04)
    if skipped:
        # Partial data beats no data, so this does not raise — but a silent skip
        # is indistinguishable from "OKX does not list it", so it is reported.
        print(f"  okx: {skipped} instruments skipped (rate limit or no funding)",
              file=sys.stderr)
    return out


def okx_future() -> list[Observation]:
    tick = _okx_tickers("FUTURES")
    out = []
    for i in _get("https://www.okx.com/api/v5/public/instruments",
                  {"instType": "FUTURES"})["data"]:
        inst = i["instId"]
        t = tick.get(inst, {})
        out.append(Observation(
            "okx", FUTURE, inst, base_asset(inst), mark=_f(t.get("last")),
            expiry=_ms_to_iso(i.get("expTime"))[:10],
            turnover_musd=_f(t.get("volCcy24h")) * _f(t.get("last")) / 1e6))
    return out


def okx_spot() -> list[Observation]:
    return [Observation("okx", SPOT, t["instId"], base_asset(t["instId"]),
                        mark=_f(t.get("last")),
                        turnover_musd=_f(t.get("volCcy24h")) / 1e6)
            for t in _okx_tickers("SPOT").values()]


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

def gate_perp() -> list[Observation]:
    contracts = _get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    tick: dict[str, dict] = {}
    try:
        tick = {t["contract"]: t for t in
                _get("https://api.gateio.ws/api/v4/futures/usdt/tickers")}
    except VenueError:
        pass
    out = []
    for c in contracts:
        name = c["name"]
        out.append(Observation(
            "gate", PERP, name, base_asset(name),
            mark=_f(c.get("mark_price")), index=_f(c.get("index_price")),
            funding_rate=_f(c.get("funding_rate")),
            funding_interval_h=_f(c.get("funding_interval"), 28800) / 3600,
            next_funding=_ms_to_iso(_f(c.get("funding_next_apply")) * 1000),
            turnover_musd=_f(tick.get(name, {}).get("volume_24h_settle")) / 1e6))
    return out


def gate_spot() -> list[Observation]:
    rows = _get("https://api.gateio.ws/api/v4/spot/tickers")
    return [Observation("gate", SPOT, r["currency_pair"],
                        base_asset(r["currency_pair"]), mark=_f(r.get("last")),
                        turnover_musd=_f(r.get("quote_volume")) / 1e6)
            for r in rows if str(r.get("currency_pair", "")).endswith(("_USDT", "_USDC"))]


def gate_future() -> list[Observation]:
    out = []
    for settle in ("usdt", "btc"):
        try:
            rows = _get(f"https://api.gateio.ws/api/v4/delivery/{settle}/contracts")
        except VenueError:
            continue
        for c in rows:
            out.append(Observation(
                "gate", FUTURE, c["name"], base_asset(c["name"]),
                mark=_f(c.get("mark_price")), index=_f(c.get("index_price")),
                expiry=_ms_to_iso(_f(c.get("expire_time")) * 1000)[:10]))
    return out


# --------------------------------------------------------------------------- #
# Mid-tier CEXs
# --------------------------------------------------------------------------- #

def bitget_perp() -> list[Observation]:
    rows = _get("https://api.bitget.com/api/v2/mix/market/tickers",
                {"productType": "USDT-FUTURES"})["data"]
    return [Observation("bitget", PERP, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice")),
                        funding_rate=_f(r.get("fundingRate")), funding_interval_h=8.0,
                        turnover_musd=_f(r.get("usdtVolume")) / 1e6,
                        oi_musd=_f(r.get("holdingAmount")) * _f(r.get("markPrice")) / 1e6)
            for r in rows]


def bitget_spot() -> list[Observation]:
    rows = _get("https://api.bitget.com/api/v2/spot/market/tickers")["data"]
    return [Observation("bitget", SPOT, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("lastPr")),
                        turnover_musd=_f(r.get("usdtVolume")) / 1e6) for r in rows]


def mexc_perp() -> list[Observation]:
    rows = _get("https://contract.mexc.com/api/v1/contract/ticker")["data"]
    return [Observation("mexc", PERP, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("lastPrice")), index=_f(r.get("indexPrice")),
                        funding_rate=_f(r.get("fundingRate")), funding_interval_h=8.0,
                        turnover_musd=_f(r.get("amount24")) / 1e6)
            for r in rows if str(r.get("symbol", "")).endswith("_USDT")]


def mexc_spot() -> list[Observation]:
    rows = _get("https://api.mexc.com/api/v3/ticker/24hr")
    return [Observation("mexc", SPOT, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("lastPrice")),
                        turnover_musd=_f(r.get("quoteVolume")) / 1e6)
            for r in rows if str(r.get("symbol", "")).endswith("USDT")]


def kucoin_perp() -> list[Observation]:
    rows = _get("https://api-futures.kucoin.com/api/v1/contracts/active")["data"]
    out = []
    for r in rows:
        if r.get("isInverse"):
            continue
        # openInterest is in CONTRACTS, and the multiplier varies per symbol
        # (0.001 on XBTUSDTM). Without it, BTC open interest reads as $1.8
        # trillion — and since the factor differs by symbol, it cannot be
        # divided back out of the archive afterwards.
        out.append(Observation(
            "kucoin", PERP, r["symbol"], base_asset(r["symbol"]),
            mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice")),
            funding_rate=_f(r.get("fundingFeeRate")),
            funding_interval_h=_f(r.get("fundingRateGranularity"),
                                  28_800_000) / 3_600_000,
            turnover_musd=_f(r.get("turnoverOf24h")) / 1e6,
            oi_musd=_f(r.get("openInterest")) * _f(r.get("multiplier"), 1.0)
            * _f(r.get("markPrice")) / 1e6))
    return out


def kucoin_spot() -> list[Observation]:
    rows = _get("https://api.kucoin.com/api/v1/market/allTickers")["data"]["ticker"]
    return [Observation("kucoin", SPOT, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("last")),
                        turnover_musd=_f(r.get("volValue")) / 1e6)
            for r in rows if str(r.get("symbol", "")).endswith(("-USDT", "-USDC"))]


def htx_perp() -> list[Observation]:
    # No contract_code parameter at all: HTX documents "if not filled in,
    # default as all", and passing "*" returns err_code 1014 with HTTP 200 —
    # which surfaced only as a KeyError and would have meant zero HTX rows,
    # forever, with nothing in the archive to show why.
    rows = _get("https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate")["data"]
    ticks: dict[str, dict] = {}
    try:
        for t in _get("https://api.hbdm.com/linear-swap-ex/market/detail/batch_merged",
                      {"business_type": "swap"})["ticks"]:
            ticks[t.get("contract_code")] = t
    except (VenueError, KeyError):
        pass
    out = []
    for r in rows:
        code = r.get("contract_code", "")
        if not str(code).endswith("USDT"):
            continue
        t = ticks.get(code, {})
        out.append(Observation("htx", PERP, code, base_asset(code),
                               mark=_f(t.get("close")),
                               funding_rate=_f(r.get("funding_rate")), funding_interval_h=8.0,
                               turnover_musd=_f(t.get("trade_turnover")) / 1e6))
    return out


def coinex_perp() -> list[Observation]:
    rows = _get("https://api.coinex.com/v2/futures/funding-rate")["data"]
    tick: dict[str, dict] = {}
    try:
        tick = {t["market"]: t for t in
                _get("https://api.coinex.com/v2/futures/ticker")["data"]}
    except (VenueError, KeyError):
        pass
    out = []
    for r in rows:
        # CoinEx defaults to 8h but dynamically moves symbols to 2h or 4h; both
        # timestamps are already in this response, so derive rather than assume.
        gap = (_f(r.get("next_funding_time")) - _f(r.get("latest_funding_time"))) / 3_600_000
        out.append(Observation(
            "coinex", PERP, r["market"], base_asset(r["market"]),
            mark=_f(r.get("mark_price")),
            funding_rate=_f(r.get("latest_funding_rate")),
            funding_interval_h=gap if 0.5 <= gap <= 24 else 8.0,
            turnover_musd=_f(tick.get(r["market"], {}).get("value")) / 1e6))
    return out


def bitmex_perp() -> list[Observation]:
    rows = _get("https://www.bitmex.com/api/v1/instrument/active")
    out = []
    for r in rows:
        if r.get("fundingRate") is None:
            continue
        # turnover24h is in the settlement currency's minor unit (satoshis for
        # XBt-settled), i.e. a BTC amount — using it as USD put every BitMEX
        # perp at ~0.00003 M and silently excluded the venue from every report.
        out.append(Observation(
            "bitmex", PERP, r["symbol"], base_asset(r["symbol"]),
            mark=_f(r.get("markPrice")), index=_f(r.get("indicativeSettlePrice")),
            funding_rate=_f(r.get("fundingRate")), funding_interval_h=8.0,
            turnover_musd=_f(r.get("foreignNotional24h")) / 1e6))
    return out


def bingx_perp() -> list[Observation]:
    rows = _get("https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex")["data"]
    # Turnover comes from a second call: premiumIndex carries no volume at all,
    # and without it every BingX row landed in the archive at 0 turnover. That
    # is not "BingX publishes no volume" — it does — and analyze.py exempts
    # zero-turnover rows from --min-turnover, so all 792 of them were surviving
    # every liquidity filter and dominating the cross-venue table.
    vols: dict[str, float] = {}
    try:
        for t in _get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker")["data"]:
            vols[t.get("symbol")] = _f(t.get("quoteVolume"))
    except (VenueError, KeyError):
        pass
    # The interval is in this very response and a third of BingX symbols are 4h;
    # assuming 8h would have halved their APR in the archive permanently.
    return [Observation("bingx", PERP, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice")),
                        funding_rate=_f(r.get("lastFundingRate")),
                        funding_interval_h=_f(r.get("fundingIntervalHours"), 8.0),
                        next_funding=_ms_to_iso(r.get("nextFundingTime")),
                        turnover_musd=vols.get(r["symbol"], 0.0) / 1e6)
            for r in rows]


# --------------------------------------------------------------------------- #
# On-chain perps — widest dispersion, and several settle hourly
# --------------------------------------------------------------------------- #

def hyperliquid_perp() -> list[Observation]:
    """Hyperliquid settles funding EVERY HOUR. Annualising it as 8h understates
    the rate eightfold — the most common error in cross-venue scans."""
    data = _request("https://api.hyperliquid.xyz/info",
                    body={"type": "metaAndAssetCtxs"})
    out = []
    for meta, ctx in zip(data[0]["universe"], data[1]):
        name = meta.get("name", "")
        # Delisted assets stay in the array (indices are stable by design), so
        # the zip alignment is safe — but their contexts are stale.
        if meta.get("isDelisted"):
            continue
        out.append(Observation(
            "hyperliquid", PERP, name, base_asset(name),
            mark=_f(ctx.get("markPx")), index=_f(ctx.get("oraclePx")),
            funding_rate=_f(ctx.get("funding")), funding_interval_h=1.0,
            turnover_musd=_f(ctx.get("dayNtlVlm")) / 1e6,
            oi_musd=_f(ctx.get("openInterest")) * _f(ctx.get("markPx")) / 1e6))
    return out


def dydx_perp() -> list[Observation]:
    """dYdX v4 also settles hourly."""
    markets = _get("https://indexer.dydx.trade/v4/perpetualMarkets")["markets"]
    # A third of dYdX markets sit in FINAL_SETTLEMENT with nextFundingRate "0".
    # Left in, they become the zero-funding LOW leg of a cross-venue spread and
    # manufacture a headline trade in a market that no longer exists.
    return [Observation("dydx", PERP, m["ticker"], base_asset(m["ticker"]),
                        mark=_f(m.get("oraclePrice")),
                        funding_rate=_f(m.get("nextFundingRate")),
                        funding_interval_h=1.0,
                        turnover_musd=_f(m.get("volume24H")) / 1e6,
                        oi_musd=_f(m.get("openInterest")) * _f(m.get("oraclePrice")) / 1e6)
            for m in markets.values() if m.get("status") == "ACTIVE"]


def paradex_perp() -> list[Observation]:
    rows = _get("https://api.prod.paradex.trade/v1/markets/summary",
                {"market": "ALL"})["results"]
    return [Observation("paradex", PERP, r["symbol"], base_asset(r["symbol"]),
                        mark=_f(r.get("mark_price")),
                        index=_f(r.get("underlying_price")),
                        funding_rate=_f(r.get("funding_rate")), funding_interval_h=8.0,
                        turnover_musd=_f(r.get("volume_24h")) / 1e6,
                        oi_musd=_f(r.get("open_interest")) * _f(r.get("mark_price")) / 1e6)
            for r in rows if str(r.get("symbol", "")).endswith("PERP")]


def backpack_perp() -> list[Observation]:
    marks = _get("https://api.backpack.exchange/api/v1/markPrices")
    vols: dict[str, float] = {}
    try:
        for t in _get("https://api.backpack.exchange/api/v1/tickers"):
            vols[t["symbol"]] = _f(t.get("quoteVolume"))
    except (VenueError, KeyError):
        pass
    # Backpack settles HOURLY, not every 8h. Treating it as 8h understated its
    # APR eightfold and made the most volatile venue in the set look like the
    # dullest — exactly backwards for a project hunting dispersion.
    return [Observation("backpack", PERP, m["symbol"], base_asset(m["symbol"]),
                        mark=_f(m.get("markPrice")), index=_f(m.get("indexPrice")),
                        funding_rate=_f(m.get("fundingRate")), funding_interval_h=1.0,
                        next_funding=_ms_to_iso(m.get("nextFundingTimestamp")),
                        turnover_musd=vols.get(m["symbol"], 0.0) / 1e6)
            for m in marks]


def deribit_all() -> list[Observation]:
    """Deribit is the deepest venue for dated crypto futures — the calendar-basis
    reference. The same call returns its perps, so both are captured."""
    out = []
    for currency in ("BTC", "ETH"):
        try:
            rows = _get(
                "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
                {"currency": currency, "kind": "future"})["result"]
            # The book summary carries no expiry, and without it every Deribit
            # future is skipped by the calendar-basis section — the one section
            # Deribit exists in this scanner to supply.
            expiries = {
                i["instrument_name"]: _ms_to_iso(i.get("expiration_timestamp"))[:10]
                for i in _get(
                    "https://www.deribit.com/api/v2/public/get_instruments",
                    {"currency": currency, "kind": "future",
                     "expired": "false"})["result"]}
        except (VenueError, KeyError):
            continue
        for r in rows:
            name = r.get("instrument_name", "")
            is_perp = name.endswith("PERPETUAL")
            out.append(Observation(
                "deribit", PERP if is_perp else FUTURE, name, currency,
                mark=_f(r.get("mark_price")),
                index=_f(r.get("estimated_delivery_price")),
                # funding_8h is the 8h-normalised rate; current_funding is the
                # instantaneous one and runs ~50% apart from it.
                funding_rate=_f(r.get("funding_8h")) if is_perp else 0.0,
                funding_interval_h=8.0 if is_perp else 0.0,
                expiry="" if is_perp else expiries.get(name, ""),
                turnover_musd=_f(r.get("volume_usd")) / 1e6,
                oi_musd=_f(r.get("open_interest")) / 1e6))
    return out


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

ADAPTERS: dict[str, dict[str, object]] = {
    # major CEXs — reference prices; several geo-block US IPs (see README)
    "binance": {PERP: binance_perp, SPOT: binance_spot, FUTURE: binance_future},
    "bybit": {PERP: bybit_perp, SPOT: bybit_spot, FUTURE: bybit_future},
    "okx": {PERP: okx_perp, SPOT: okx_spot, FUTURE: okx_future},
    "gate": {PERP: gate_perp, SPOT: gate_spot, FUTURE: gate_future},
    "bitget": {PERP: bitget_perp, SPOT: bitget_spot},
    # mid tier — where dispersion starts
    "mexc": {PERP: mexc_perp, SPOT: mexc_spot},
    "kucoin": {PERP: kucoin_perp, SPOT: kucoin_spot},
    "htx": {PERP: htx_perp},
    "coinex": {PERP: coinex_perp},
    "bitmex": {PERP: bitmex_perp},
    "bingx": {PERP: bingx_perp},
    # on-chain perps — widest dispersion, hourly funding on some
    "hyperliquid": {PERP: hyperliquid_perp},
    "dydx": {PERP: dydx_perp},
    "paradex": {PERP: paradex_perp},
    "backpack": {PERP: backpack_perp},
    # dated futures reference — registered under both kinds because one call
    # returns both, and registering only FUTURE meant `--kinds perp` silently
    # collected nothing from Deribit at all
    "deribit": {FUTURE: deribit_all, PERP: deribit_all},
}

ALL_VENUES = list(ADAPTERS)
