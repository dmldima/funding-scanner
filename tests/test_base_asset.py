#!/usr/bin/env python3
"""Tests for base_asset(), the function every cross-venue comparison depends on.

A miss here is invisible: a symbol that normalises to the wrong base does not
raise, it just silently stops matching its counterpart on another venue, and the
row disappears from a section that still reports itself as complete. That has
now happened three times (kPEPE/KAVA, TUSDUSDT, and OKX's _UM contracts), which
is why this file exists.

Run:  python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from venues import base_asset


class TestPlainSymbols(unittest.TestCase):
    def test_common_shapes(self):
        for raw, want in [
            ("BTCUSDT", "BTC"),
            ("BTC-USDT-SWAP", "BTC"),
            ("BTC-USDT", "BTC"),
            ("BTC_USDT", "BTC"),
            ("XBTUSDTM", "BTC"),          # KuCoin: XBT alias + M suffix
            ("BTC-PERP", "BTC"),
            ("BTC-USD-PERPETUAL", "BTC"),
            ("BTCUSDT_261225", "BTC"),    # dated: expiry stripped
            ("ETH-USDC", "ETH"),
            ("SOLUSDCPERP", "SOL"),       # stacked markers
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)


class TestOkxSettlementMarkers(unittest.TestCase):
    """OKX linear futures: _UM, and _UM_XPERP for the long-dated ones.

    Regression test. These normalised to BTCUSDUM / NVDAUSDUMX, which matched no
    spot row, so 109 of 179 futures rows were silently dropped from the calendar
    basis section while it presented itself as complete.
    """

    def test_dated_linear(self):
        for raw, want in [
            ("BTC-USD_UM-260925", "BTC"),
            ("ETH-USD_UM-260731", "ETH"),
            ("SOL-USD_UM-270326", "SOL"),
            ("XAU-USD_UM-261225", "XAU"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)

    def test_long_dated_xperp(self):
        for raw, want in [
            ("BTC-USD_UM_XPERP-310404", "BTC"),
            ("NVDA-USD_UM_XPERP-310613", "NVDA"),
            ("SOFTBANK-USD_UM_XPERP-310704", "SOFTBANK"),
            ("O-USD_UM_XPERP-310620", "O"),        # single-letter ticker
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)

    def test_inverse_still_works(self):
        self.assertEqual(base_asset("BTC-USD-260925"), "BTC")

    def test_marker_needs_a_delimiter(self):
        """The bare letters UM/XPERP must never be stripped off a real ticker.

        This is why the marker is removed before separators are, and not via
        _KIND_SUFFIX: a bare "UM" rule applied after separator removal would
        turn PLATINUM into PLATIN and GUM into G.
        """
        for raw, want in [
            ("PLATINUMUSDT", "PLATINUM"),
            ("GUM-USDT", "GUM"),
            ("UM-USDT", "UM"),
            ("XPERPUSDT", "XPERP"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)


class TestQuoteVariants(unittest.TestCase):
    """USDT and USDC contracts of one asset must reduce to the same base, or the
    two legs never pair up in a cross-venue comparison. Each venue spells them
    differently, and several were being skipped entirely before these were
    covered."""

    def test_usdc_shapes_match_usdt(self):
        for usdt, usdc in [
            ("BTCUSDT", "BTCUSDC"),          # binance / bybit
            ("BTC_USDT", "BTC_USDC"),        # mexc, gate
            ("BTC-USDT", "BTC-USDC"),        # bingx, kucoin spot
            ("BTCUSDTM", "BTCUSDCM"),        # kucoin perp
        ]:
            with self.subTest(pair=(usdt, usdc)):
                self.assertEqual(base_asset(usdt), base_asset(usdc))
                self.assertEqual(base_asset(usdc), "BTC")

    def test_bitget_usdc_product_type(self):
        """USDC-FUTURES drops the quote entirely: BTCPERP, not BTCUSDC."""
        for raw, want in [("BTCPERP", "BTC"), ("ETHPERP", "ETH"),
                          ("PEPEPERP", "PEPE"), ("DOGEPERP", "DOGE")]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)

    def test_whitebit_perp_suffix(self):
        for raw, want in [("BTC_PERP", "BTC"), ("0G_PERP", "0G"),
                          ("1INCH_PERP", "1INCH"), ("XAU_PERP", "XAU")]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)

    def test_stablecoin_quoted_in_another_stablecoin(self):
        """USDCUSDT is the USDC perp quoted in USDT — base USDC, not empty."""
        self.assertEqual(base_asset("USDCUSDT"), "USDC")

    def test_usd1_is_a_quote_currency(self):
        """USD1 is a stablecoin quote on Aster and MEXC, not a numbered
        contract. Without it in _QUOTES, ETHUSD1 keeps the whole string and
        never pairs with ETH on any other venue."""
        for raw, want in [("ETHUSD1", "ETH"), ("SOLUSD1", "SOL"),
                          ("XAGUSD1", "XAG"), ("BTC_USD1", "BTC"),
                          ("USDCUSD1", "USDC"), ("USDTUSD1", "USDT")]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)

    def test_usd1_ordering_against_usd(self):
        """USD1USD is USD1 quoted in USD, so USD is what comes off. Reversing
        the order in _QUOTES would leave the whole string."""
        self.assertEqual(base_asset("USD1USD"), "USD1")
        self.assertEqual(base_asset("USD1"), "USD1")

    def test_kraken_and_extended_shapes(self):
        """Kraken prefixes linear perps PF_ (stripped by the adapter, not here);
        Extended names them ASSET-USD."""
        for raw, want in [("XBTUSD", "BTC"), ("ETHUSD", "ETH"),
                          ("BTC-USD", "BTC"), ("HYPE-USD", "HYPE"),
                          ("USDJPY-USD", "USDJPY")]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)


class TestMultipliers(unittest.TestCase):
    """1000PEPE and PEPE are the same asset; their funding rates are directly
    comparable, so the multiplier must go or cross-venue pairing breaks."""

    def test_prefix_and_suffix(self):
        for raw, want in [
            ("1000PEPEUSDT", "PEPE"),
            ("PEPE1000USDT", "PEPE"),
            ("1000000BABYDOGE_USDT", "BABYDOGE"),
            ("10000SATS-USDT", "SATS"),
            ("1000BONKUSDTM", "BONK"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)

    def test_lowercase_k_before_upper(self):
        """kPEPE must lose the k BEFORE .upper(), or it is indistinguishable
        from a real leading K and KAVA becomes AVA — which then collides with
        the genuine AVA token and prints a spread between unrelated assets."""
        self.assertEqual(base_asset("kPEPE"), "PEPE")
        self.assertEqual(base_asset("kBONK"), "BONK")
        self.assertEqual(base_asset("kPEPE-USD"), "PEPE")
        self.assertEqual(base_asset("KAVA"), "KAVA")
        self.assertEqual(base_asset("KAVAUSDT"), "KAVA")

    def test_digits_in_names_survive(self):
        """Only real contract multipliers, never a bare \\d+$: these are ticker
        names, and a greedy rule turns them into L, G, M and X2Y."""
        for raw in ("L3", "G3", "M87", "X2Y2"):
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), raw)


class TestQuoteStrippedOnce(unittest.TestCase):
    """Looping until stable turns TUSDUSDT into T (colliding with Threshold),
    BUSD into B and PYUSD into PY."""

    def test_stablecoin_names(self):
        for raw, want in [
            ("TUSDUSDT", "TUSD"),
            ("BUSDUSDT", "BUSD"),
            ("PYUSDUSDT", "PYUSD"),
            ("USDCUSDT", "USDC"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(base_asset(raw), want)


class TestDegenerateInput(unittest.TestCase):
    def test_bare_quote_currency_is_not_eaten(self):
        """A symbol that is nothing but a quote currency or marker must not
        normalise to the empty string — an empty base would group every such
        row together. The `s or raw.upper()` fallback is what prevents this.

        Empty input is not covered: it returns "" and no adapter produces it.
        """
        for raw in ("USDT", "USD", "PERP", "SWAP"):
            with self.subTest(raw=raw):
                self.assertNotEqual(base_asset(raw), "")

    def test_whitespace_and_case(self):
        self.assertEqual(base_asset("  btcusdt  "), "BTC")




class TestTickerCollision(unittest.TestCase):
    """Two different assets can wear the same ticker across venues, and the
    price is what gives it away: contract multipliers are exact powers of ten,
    a machinery manufacturer priced against a memecoin is not."""

    @staticmethod
    def _row(venue, symbol, mark):
        from analyze import Row
        return Row("t", venue, "perp", symbol, "CAT", "", mark, 0, 0, 8, 10, 0)

    def test_caterpillar_is_not_the_cat_memecoin(self):
        from analyze import same_asset
        legs = [self._row("mexc", "CAT_USDT", 1.372e-06),
                self._row("bitget", "1000CATUSDT", 0.001378),
                self._row("kucoin", "10000CATUSDTM", 0.01377),
                self._row("bingx", "1000CAT-USDT", 0.001377),
                self._row("whitebit", "CAT_PERP", 883.17)]     # Caterpillar Inc.
        kept = same_asset(legs)
        self.assertEqual(len(kept), 4)
        self.assertNotIn("whitebit", [r.venue for r in kept])

    def test_real_multipliers_survive(self):
        """kPEPE is 1000x PEPE and the two ARE comparable — dropping them
        would break exactly the pairing base_asset exists to enable."""
        from analyze import same_asset
        legs = [self._row("bitget", "PEPEUSDT", 2.94e-06),
                self._row("kucoin", "PEPEUSDTM", 2.94e-06),
                self._row("hyperliquid", "kPEPE", 0.00294)]
        self.assertEqual(len(same_asset(legs)), 3)

    def test_single_leg_untouched(self):
        from analyze import same_asset
        legs = [self._row("mexc", "CAT_USDT", 1.372e-06)]
        self.assertEqual(same_asset(legs), legs)


if __name__ == "__main__":
    unittest.main()
