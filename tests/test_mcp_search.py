import json
import os
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

from src.db import init_db, upsert_asset, search_assets
from src.mcp_server import search_owned_assets


class TestSearchOwnedAssetsEmptyQuery(unittest.TestCase):
    """An empty query lists the vault via search_assets() instead of hybrid search.
    It used to crash on the unassigned `note`, and it applied local_only/source/...
    only after fetching the first N rows by title, so later-sorting matches vanished."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)
        # More cloud-only assets than the default limit (25), all sorting before the local ones
        for i in range(30):
            upsert_asset({"id": f"cloud_{i}", "source": "quixel",
                          "title": f"A Cloud Asset {i:02d}"}, db_path=self.db_path)
        for i in range(3):
            upsert_asset({"id": f"local_{i}", "source": "unity", "title": f"Z Local Asset {i}",
                          "local_path": f"/vault/local_{i}.unitypackage"}, db_path=self.db_path)

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _search(self, **kwargs):
        with patch("src.mcp_server.search_assets", partial(search_assets, db_path=self.db_path)):
            return json.loads(search_owned_assets(**kwargs))

    def test_empty_query_local_only_lists_all_local_assets(self):
        for query in ("", "   "):
            with self.subTest(query=query):
                out = self._search(query=query, local_only=True)
                self.assertEqual(out["count"], 3)
                self.assertEqual(sorted(r["id"] for r in out["results"]), ["local_0", "local_1", "local_2"])

    def test_empty_query_source_filter_stays_case_insensitive(self):
        out = self._search(query="", source="Unity")
        self.assertEqual(out["count"], 3)
        self.assertEqual(sorted(r["id"] for r in out["results"]), ["local_0", "local_1", "local_2"])


if __name__ == "__main__":
    unittest.main()
