#!/usr/bin/env python3
"""Tests for percentile() and the derived round-trip constant.

Both replace literals that were quietly wrong: an index-based percentile that
returns the maximum at small n, and a 0.31 hardcoded beside the fee table it
was derived from.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyze import DEFAULT_FEE, ROUND_TRIP_PCT, SPOT_FEE_PCT, percentile


class TestPercentile(unittest.TestCase):
    def test_the_bug_it_replaces(self):
        """values[int(n*0.75)] returns the maximum at n=4 and calls it the
        75th percentile. The real answer sits between the 3rd and 4th value."""
        d = [1.0, 2.0, 3.0, 100.0]
        self.assertEqual(d[int(len(d) * 0.75)], 100.0)     # the old behaviour
        self.assertEqual(percentile(d, 0.75), 27.25)       # 3 + 0.75*(100-3)

    def test_known_quantiles(self):
        d = [0.0, 1.0, 2.0, 3.0, 4.0]
        self.assertEqual(percentile(d, 0.0), 0.0)
        self.assertEqual(percentile(d, 0.5), 2.0)
        self.assertEqual(percentile(d, 0.75), 3.0)
        self.assertEqual(percentile(d, 1.0), 4.0)

    def test_interpolates_between_points(self):
        self.assertEqual(percentile([0.0, 10.0], 0.5), 5.0)
        self.assertEqual(percentile([0.0, 10.0], 0.25), 2.5)

    def test_degenerate_inputs(self):
        self.assertEqual(percentile([], 0.75), 0.0)
        self.assertEqual(percentile([7.5], 0.75), 7.5)

    def test_never_exceeds_the_data(self):
        d = [1.0, 2.0, 3.0, 100.0]
        for q in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0):
            with self.subTest(q=q):
                self.assertGreaterEqual(percentile(d, q), min(d))
                self.assertLessEqual(percentile(d, q), max(d))


class TestRoundTripConstant(unittest.TestCase):
    def test_derived_from_the_fee_table(self):
        """Was hardcoded 0.31 next to the fees it came from. Same value, but
        it now follows a fee edit instead of silently disagreeing with one."""
        self.assertAlmostEqual(ROUND_TRIP_PCT, 0.31)
        self.assertAlmostEqual(ROUND_TRIP_PCT, SPOT_FEE_PCT * 2 + DEFAULT_FEE * 2)


if __name__ == "__main__":
    unittest.main()
