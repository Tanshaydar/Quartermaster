import os
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

from src.db import init_db, upsert_asset, search_assets
from src.semantic import hybrid_search
from src.server import api_assets

# (id, claimed_date, size_mb, query-term hits). More hits rank higher, so relevance order is the
# reverse of title order, and each sort mode has one tie that only the title tie-break settles.
SORT_ROWS = [("alpha", "2021-01-01", 5.0, 1), ("bravo", "2023-06-15", 1.0, 2),
             ("charlie", "2023-06-15", 250.0, 3), ("delta", "", 5.0, 4)]


class TestApiAssetsQueryFilters(unittest.TestCase):
    """/api/assets (web UI, Unity editor bridge) ran its filters on the output of hybrid_search(),
    which keeps only each signal's top 50 hits, so matches ranked past that cap vanished. Its local
    filter also tested for 'true'/'false' while the web UI sends 'local'/'cloud', so it never applied."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)
        # 60 cloud-only Unity decoys say "rock" twice (title and tags), so BM25 ranks every one
        # of them above the target and they fill the keyword signal's top 50
        for i in range(60):
            upsert_asset({"id": f"decoy_{i:02d}", "source": "unity", "title": f"Rock Decoy {i:02d}",
                          "tags": ["rock"]}, db_path=self.db_path)
        upsert_asset({"id": "fab_0", "source": "fab", "title": "Rock Target Fab 0", "category": "Terrain & Landscape",
                      "local_path": "/vault/fab_0.zip"}, db_path=self.db_path)

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _get(self, query="rock", **params):
        with patch("src.server.search_assets", partial(search_assets, db_path=self.db_path)), \
                patch("src.server.semantic.hybrid_search", partial(hybrid_search, db_path=self.db_path)), \
                patch("src.server.get_stats", return_value={}):
            return api_assets(query=query, limit=2000, **params)

    def test_filters_keep_matches_ranked_past_the_hybrid_cap(self):
        for params in (dict(source="fab"), dict(category="Terrain & Landscape"), dict(local="local")):
            with self.subTest(**params):
                out = self._get(**params)
                self.assertEqual([it["id"] for it in out["items"]], ["fab_0"])
                self.assertEqual(out["total"], 1)

    def test_cloud_filter_drops_local_assets(self):
        self.assertEqual([it["id"] for it in self._get(query="target")["items"]], ["fab_0"])
        self.assertEqual(self._get(query="target", local="cloud")["items"], [])


class TestApiAssetsQuerySort(unittest.TestCase):
    """With a query, /api/assets sorted the hybrid results on claimed_at / size_bytes, keys asset
    rows don't have, so "claimed_desc" and "size_desc" silently kept relevance order."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)
        for aid, claimed, size_mb, hits in SORT_ROWS:
            upsert_asset({"id": aid, "title": f"{aid.title()} Crate", "claimed_date": claimed,
                          "size_mb": size_mb, "summary": " ".join(["crate"] * hits)}, db_path=self.db_path)

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _get(self, query="crate", **params):
        with patch("src.server.search_assets", partial(search_assets, db_path=self.db_path)), \
                patch("src.server.semantic.hybrid_search", partial(hybrid_search, db_path=self.db_path)), \
                patch("src.server.get_stats", return_value={}):
            return api_assets(query=query, **params)

    def test_query_results_follow_sort_by(self):
        expected = {
            "relevance": ["delta", "charlie", "bravo", "alpha"],     # hybrid order, kept as is
            "claimed_desc": ["bravo", "charlie", "alpha", "delta"],  # newest first, undated last
            "size_desc": ["charlie", "alpha", "delta", "bravo"],     # largest first
        }
        for sort_by, ids in expected.items():
            with self.subTest(sort_by=sort_by):
                out = self._get(sort_by=sort_by)
                self.assertEqual(out["search_mode"], "keyword-only")   # hybrid branch, not the SQL fallback
                self.assertEqual([it["id"] for it in out["items"]], ids)


if __name__ == "__main__":
    unittest.main()
