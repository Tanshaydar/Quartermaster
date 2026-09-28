import os
import sqlite3
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

from src.db import init_db, search_assets, upsert_asset
from src.semantic import hybrid_search

try:
    from src.desktop import SearchWorker
    _QT_ERROR = None
except (ImportError, OSError, RuntimeError) as e:
    SearchWorker, _QT_ERROR = None, e

LOCAL_IDS = ["local_0", "local_1", "local_2"]
DECOY_IDS = [f"decoy_{i:02d}" for i in range(60)]
TARGET_IDS = ["fab_0", "unity_0", "unity_1"]
# (id, claimed_date, size_mb, query-term hits). More hits rank higher, so relevance order is the
# reverse of title order, and each sort mode has one tie that only the title tie-break settles.
SORT_ROWS = [("alpha", "2021-01-01", 5.0, 1), ("bravo", "2023-06-15", 1.0, 2),
             ("charlie", "2023-06-15", 250.0, 3), ("delta", "", 5.0, 4)]


@unittest.skipIf(_QT_ERROR is not None, f"Qt GUI runtime not available: {_QT_ERROR}")
class TestSearchWorkerLocalOnly(unittest.TestCase):
    """"Local only" was applied in Python after search_assets() had already cut the vault
    to its first 5000 rows (and after hybrid_search() had kept only its top hits), so
    downloaded assets that sorted past the cap silently vanished from the desktop list."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "test_assets.db")
        init_db(cls.db_path)
        # 5000 cloud-only assets that sort ahead of the local ones by title, claimed date and size.
        # Bulk insert: one upsert_asset() commit per row would make setup take seconds.
        rows = [(f"cloud_{i}", "quixel", f"A Cloud Asset {i:04d}", "2024-01-01", 10.0, "") for i in range(5000)]
        rows += [(aid, "unity", f"Z Local Asset {i}", "2020-01-01", 1.0, f"/vault/{aid}.unitypackage")
                 for i, aid in enumerate(LOCAL_IDS)]
        conn = sqlite3.connect(cls.db_path)
        conn.executemany("INSERT INTO assets (id, source, title, claimed_date, size_mb, local_path) "
                         "VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT INTO assets_fts (id, title) SELECT id, title FROM assets")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        import gc
        gc.collect()
        try:
            cls.tmpdir.cleanup()
        except Exception:
            pass

    def _run(self, query="", sort_mode="relevance", local_only=True):
        """Run the worker synchronously (no event loop) and return its single emission."""
        emitted = []
        worker = SearchWorker(1, query, None, None, None, sort_mode, local_only=local_only)
        worker.results_ready.connect(lambda query_id, items, mode: emitted.append((items, mode)))
        with patch("src.desktop.search_assets", partial(search_assets, db_path=self.db_path)):
            worker.run()
        self.assertEqual(len(emitted), 1)
        return emitted[0]

    def test_browse_local_only_lists_every_local_asset(self):
        for sort_mode in ("relevance", "title_asc", "claimed_desc", "size_desc"):
            with self.subTest(sort_mode=sort_mode):
                items, mode = self._run(sort_mode=sort_mode)
                self.assertEqual(mode, "browse")
                self.assertEqual(sorted(it["id"] for it in items), LOCAL_IDS)

    def test_browse_is_not_capped(self):
        items, _ = self._run(local_only=False)
        self.assertEqual(len(items), 5003)

    def test_keyword_fallback_local_only_lists_every_local_match(self):
        with patch("src.desktop.semantic.hybrid_search", side_effect=RuntimeError("model unavailable")):
            items, mode = self._run(query="asset", sort_mode="claimed_desc")
        self.assertEqual(mode, "keyword")
        self.assertEqual(sorted(it["id"] for it in items), LOCAL_IDS)

    def test_hybrid_local_only_adds_local_keyword_hits_past_its_cap(self):
        # hybrid_search keeps only each signal's top hits; here one local asset made the cut
        capped = [{"id": "local_1", "title": "Z Local Asset 1", "local_path": "/vault/local_1.unitypackage"}]
        fake = {"results": capped, "search_mode": "3-way-hybrid"}
        with patch("src.desktop.semantic.hybrid_search", return_value=fake) as hybrid:
            items, mode = self._run(query="asset")
        self.assertEqual(hybrid.call_args.kwargs["local"], "local")   # filtered before the cap
        self.assertEqual(mode, "3-way-hybrid")
        ids = [it["id"] for it in items]
        self.assertEqual(sorted(ids), LOCAL_IDS)   # the rest are appended, without duplicates
        self.assertEqual(ids[0], "local_1")        # hybrid-ranked hits keep their place


@unittest.skipIf(_QT_ERROR is not None, f"Qt GUI runtime not available: {_QT_ERROR}")
class TestSearchWorkerFilters(unittest.TestCase):
    """With a query, the engine chip, category and pipeline filters ran in Python on the output of
    hybrid_search(), which keeps only each signal's top 50 hits, so every match ranked past that
    cap vanished (on the real vault, "wall" with the Fab chip listed 0 of its 51 Fab matches)."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "test_assets.db")
        init_db(cls.db_path)
        # 60 Quixel decoys say "rock" twice (title and tags), so BM25 ranks every one of them
        # above the targets and they fill the keyword signal's top 50
        for aid in DECOY_IDS:
            upsert_asset({"id": aid, "source": "quixel", "title": f"Rock Decoy {aid[-2:]}", "tags": ["rock"],
                          "category": "3D Environments & Props", "render_pipelines": ["HDRP"]},
                         db_path=cls.db_path)
        # A Fab listing has no render pipelines; its pipeline shows up in its formats
        upsert_asset({"id": "fab_0", "source": "fab", "title": "Rock Target Fab 0", "category": "Terrain & Landscape",
                      "render_pipelines": [], "formats": ["URP"]}, db_path=cls.db_path)
        for i in range(2):
            upsert_asset({"id": f"unity_{i}", "source": "unity", "title": f"Rock Target Unity {i}",
                          "category": "Terrain & Landscape", "render_pipelines": ["URP"]}, db_path=cls.db_path)

    @classmethod
    def tearDownClass(cls):
        import gc
        gc.collect()
        try:
            cls.tmpdir.cleanup()
        except Exception:
            pass

    def _run(self, query="rock", eng=None, pipe=None, cat=None):
        """Run the worker synchronously against the temp vault and return the listed ids."""
        emitted = []
        worker = SearchWorker(1, query, eng, pipe, cat, "relevance")
        worker.results_ready.connect(lambda query_id, items, mode: emitted.append(items))
        with patch("src.desktop.search_assets", partial(search_assets, db_path=self.db_path)), \
                patch("src.desktop.semantic.hybrid_search", partial(hybrid_search, db_path=self.db_path)):
            worker.run()
        self.assertEqual(len(emitted), 1)
        return [it["id"] for it in emitted[0]]

    def test_filters_list_matches_ranked_past_the_hybrid_cap(self):
        cases = [(dict(eng="fab"), ["fab_0"]),
                 (dict(eng="unity"), ["unity_0", "unity_1"]),
                 (dict(cat="Terrain & Landscape"), TARGET_IDS),
                 (dict(pipe="URP"), TARGET_IDS)]   # fab_0 through its formats
        for query in ("rock", ""):   # search and browse agree on what each filter matches
            for filters, expected in cases:
                with self.subTest(query=query, **filters):
                    self.assertEqual(sorted(self._run(query=query, **filters)), expected)

    def test_filtered_search_lists_every_match_hybrid_ranked_first(self):
        ranked = [it["id"] for it in hybrid_search("rock", limit=5000, source="quixel",
                                                   db_path=self.db_path)["results"]]
        self.assertEqual(len(ranked), 50)   # the keyword signal's cap
        ids = self._run(eng="quixel")
        self.assertEqual(sorted(ids), DECOY_IDS)            # all 60, each once
        self.assertEqual(ids[:len(ranked)], ranked)         # hybrid order first, the rest after


@unittest.skipIf(_QT_ERROR is not None, f"Qt GUI runtime not available: {_QT_ERROR}")
class TestSearchWorkerQuerySort(unittest.TestCase):
    """With a query, "Recently Acquired" and "Size (Largest)" sorted the hybrid results on
    claimed_at / size_bytes, keys asset rows don't have, so both silently kept relevance order."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "test_assets.db")
        init_db(cls.db_path)
        for aid, claimed, size_mb, hits in SORT_ROWS:
            upsert_asset({"id": aid, "title": f"{aid.title()} Crate", "claimed_date": claimed,
                          "size_mb": size_mb, "summary": " ".join(["crate"] * hits)}, db_path=cls.db_path)

    @classmethod
    def tearDownClass(cls):
        import gc
        gc.collect()
        try:
            cls.tmpdir.cleanup()
        except Exception:
            pass

    def _run(self, query, sort_mode):
        """Run the worker synchronously (no event loop) and return its single emission."""
        emitted = []
        worker = SearchWorker(1, query, None, None, None, sort_mode)
        worker.results_ready.connect(lambda query_id, items, mode: emitted.append((items, mode)))
        with patch("src.desktop.search_assets", partial(search_assets, db_path=self.db_path)), \
                patch("src.desktop.semantic.hybrid_search", partial(hybrid_search, db_path=self.db_path)):
            worker.run()
        self.assertEqual(len(emitted), 1)
        return emitted[0]

    def test_query_results_follow_sort_mode(self):
        expected = {
            "relevance": ["delta", "charlie", "bravo", "alpha"],     # hybrid order, kept as is
            "claimed_desc": ["bravo", "charlie", "alpha", "delta"],  # newest first, undated last
            "size_desc": ["charlie", "alpha", "delta", "bravo"],     # largest first
        }
        for sort_mode, ids in expected.items():
            with self.subTest(sort_mode=sort_mode):
                items, mode = self._run("crate", sort_mode)
                self.assertEqual(mode, "keyword-only")   # hybrid_search (no vectors), not the SQL fallback
                self.assertEqual([it["id"] for it in items], ids)


if __name__ == "__main__":
    unittest.main()
