import os
import sqlite3
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

from src.db import init_db, search_assets

try:
    from src.desktop import SearchWorker
    _QT_ERROR = None
except (ImportError, OSError, RuntimeError) as e:
    SearchWorker, _QT_ERROR = None, e

LOCAL_IDS = ["local_0", "local_1", "local_2"]


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
        capped = [{"id": f"cloud_{i}", "title": f"A Cloud Asset {i:04d}", "local_path": ""} for i in range(50)]
        capped.insert(10, {"id": "local_1", "title": "Z Local Asset 1", "local_path": "/vault/local_1.unitypackage"})
        fake = {"results": capped, "search_mode": "3-way-hybrid"}
        with patch("src.desktop.semantic.hybrid_search", return_value=fake):
            items, mode = self._run(query="asset")
        self.assertEqual(mode, "3-way-hybrid")
        ids = [it["id"] for it in items]
        self.assertEqual(sorted(ids), LOCAL_IDS)   # the rest are appended, without duplicates
        self.assertEqual(ids[0], "local_1")        # hybrid-ranked hits keep their place


if __name__ == "__main__":
    unittest.main()
