import json
import os
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

from src.db import init_db, upsert_asset, search_assets, get_connection
from src.semantic import hybrid_search
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


class TestSearchOwnedAssetsQueryFilters(unittest.TestCase):
    """With a query, every filter ran on the output of hybrid_search(), which keeps only each
    signal's top 50 hits, so matches ranked past that cap vanished (on the real vault,
    query="wall" with source="fab" returned nothing although 51 Fab assets match)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)
        # 60 Unity decoys say "rock" twice (title and tags), so BM25 ranks every one of them
        # above the targets and they fill the keyword signal's top 50
        for i in range(60):
            upsert_asset({"id": f"decoy_{i:02d}", "source": "unity", "title": f"Rock Decoy {i:02d}",
                          "tags": ["rock"], "render_pipelines": ["HDRP"]}, db_path=self.db_path)
        upsert_asset({"id": "fab_0", "source": "fab", "title": "Rock Target Fab 0", "render_pipelines": ["URP"],
                      "category": "Terrain & Landscape", "local_path": "/vault/fab_0.zip"}, db_path=self.db_path)
        upsert_asset({"id": "quixel_0", "source": "quixel", "title": "Rock Target Quixel 0",
                      "render_pipelines": ["HDRP"], "formats": ["Unreal Engine", "FBX", "Textures"]},
                     db_path=self.db_path)

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _search(self, **kwargs):
        with patch("src.mcp_server.search_assets", partial(search_assets, db_path=self.db_path)), \
                patch("src.mcp_server.semantic.hybrid_search", partial(hybrid_search, db_path=self.db_path)), \
                patch("src.mcp_server.get_connection", partial(get_connection, db_path=self.db_path)):
            return json.loads(search_owned_assets(query="rock", **kwargs))

    def test_filters_keep_matches_ranked_past_the_hybrid_cap(self):
        cases = [(dict(source="fab"), ["fab_0"]),
                 (dict(source="Fab"), ["fab_0"]),
                 (dict(engine="unreal"), ["fab_0", "quixel_0"]),   # Unity packages don't port to Unreal
                 (dict(engine="godot"), ["quixel_0"]),             # Quixel FBX/textures do
                 (dict(pipeline="URP"), ["fab_0"]),
                 (dict(category="Terrain & Landscape"), ["fab_0"]),
                 (dict(local_only=True), ["fab_0"])]
        for filters, expected in cases:
            with self.subTest(**filters):
                out = self._search(**filters)
                self.assertEqual(sorted(r["id"] for r in out["results"]), expected)
                self.assertEqual(out["count"], len(expected))

    def test_unfiltered_query_still_ranks_the_whole_vault(self):
        out = self._search()
        self.assertEqual(len(out["results"]), 25)
        self.assertTrue(all(r["id"].startswith("decoy_") for r in out["results"]))


if __name__ == "__main__":
    unittest.main()
