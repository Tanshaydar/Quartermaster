import os
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

from src.db import init_db, upsert_asset, search_assets
from src.semantic import hybrid_search
from src.server import api_assets


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


if __name__ == "__main__":
    unittest.main()
