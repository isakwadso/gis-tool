"""Tests for the 10-year pasture filter. Needs shapely (installed in the pasture workflow).

Run: python -m unittest discover -s scripts -p "test_pasture.py"
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from shapely.geometry import box
    import build_pasture
except ImportError:  # the forecast workflow runs without shapely
    build_pasture = None

X, Y = 400_000, 6_150_000  # SWEREF 99 TM, somewhere in Skåne


def block(bid, x0, y0, size=100.0):
    return {"type": "Feature", "properties": {"blockid": bid},
            "geometry": {"type": "Polygon", "coordinates": [[
                [X + x0, Y + y0], [X + x0 + size, Y + y0], [X + x0 + size, Y + y0 + size],
                [X + x0, Y + y0 + size], [X + x0, Y + y0]]]}}


@unittest.skipIf(build_pasture is None, "shapely not installed")
class HistoryFilterTests(unittest.TestCase):
    years = [2016, 2017, 2018]

    def run_filter(self, features, ids_by_year, polys_by_year, overlap=0.5):
        loaded = []

        def load(y):
            loaded.append(y)
            return polys_by_year.get(y, [])
        kept, stats = build_pasture.filter_by_history(features, self.years, ids_by_year, load, overlap)
        return [f["properties"]["blockid"] for f in kept], stats, loaded

    def test_same_id_every_year_is_kept_without_geometry(self):
        feats = [block("a", 0, 0)]
        ids = {2016: {"a"}, 2017: {"a"}, 2018: {"a"}}
        kept, stats, loaded = self.run_filter(feats, ids, {})
        self.assertEqual(kept, ["a"])
        self.assertEqual(stats["kept_by_id"], 1)
        self.assertEqual(loaded, [])

    def test_redrawn_block_kept_by_location(self):
        feats = [block("new", 0, 0)]
        ids = {2016: {"old"}, 2017: {"old"}, 2018: {"new"}}
        old = [box(X - 10, Y - 10, X + 90, Y + 110)]   # covers 90 % of the new block
        kept, stats, loaded = self.run_filter(feats, ids, {2016: old, 2017: old})
        self.assertEqual(kept, ["new"])
        self.assertEqual(stats["kept_by_location"], 1)
        self.assertEqual(loaded, [2016, 2017])

    def test_new_pasture_on_former_field_dropped(self):
        feats = [block("young", 0, 0)]
        ids = {2016: set(), 2017: {"young"}, 2018: {"young"}}
        elsewhere = [box(X + 500, Y + 500, X + 600, Y + 600)]
        kept, stats, _ = self.run_filter(feats, ids, {2016: elsewhere})
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped"], 1)

    def test_partial_overlap_below_threshold_dropped(self):
        feats = [block("b", 0, 0)]
        ids = {2016: set(), 2017: {"b"}, 2018: {"b"}}
        third = [box(X, Y, X + 30, Y + 100)]           # covers 30 %
        kept, _, _ = self.run_filter(feats, ids, {2016: third})
        self.assertEqual(kept, [])

    def test_mixed(self):
        feats = [block("a", 0, 0), block("new", 200, 0), block("young", 400, 0)]
        ids = {2016: {"a", "gone"}, 2017: {"a", "new"}, 2018: {"a", "new", "young"}}
        old = [box(X + 200, Y, X + 300, Y + 100)]
        kept, stats, loaded = self.run_filter(feats, ids, {2016: old, 2017: []})
        self.assertEqual(sorted(kept), ["a", "new"])
        self.assertEqual((stats["kept_by_id"], stats["kept_by_location"], stats["dropped"]), (1, 1, 1))
        # "young" already fails in 2016, so 2017 outlines are never downloaded
        self.assertEqual(loaded, [2016])


if __name__ == "__main__":
    unittest.main()
