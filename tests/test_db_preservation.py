import os
import unittest
import tempfile
from src.db import init_db, upsert_asset, mark_enriched, get_connection, search_assets


class TestDbPreservation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_upsert_preservation(self):
        test_id = "test_preservation_001"
        
        # 1. Initial raw store fetch
        upsert_asset({
            "id": test_id,
            "source": "unity",
            "title": "Hyper Realistic Ocean Water System",
            "summary": "Initial short summary",
            "usage_notes": "Initial short notes",
            "enriched": 0
        }, db_path=self.db_path)

        # 2. Enriched with deep metadata
        mark_enriched(test_id,
                      summary="High performance compute-shader FFT ocean simulation with buoyancy",
                      usage_notes="Requires compute shader support; HDRP ready",
                      video_links=["https://youtube.com/watch?v=ocean123456"],
                      db_path=self.db_path)

        # 3. Store re-fetch with empty / fallback summary
        upsert_asset({
            "id": test_id,
            "source": "unity",
            "title": "Hyper Realistic Ocean Water System",
            "summary": "Fallback summary from classifier",
            "usage_notes": "Fallback notes",
            "enriched": 0
        }, db_path=self.db_path)

        conn = get_connection(self.db_path)
        row = conn.execute("SELECT summary, usage_notes, video_links, enriched FROM assets WHERE id = ?", (test_id,)).fetchone()
        self.assertEqual(row["summary"], "High performance compute-shader FFT ocean simulation with buoyancy")
        self.assertEqual(row["usage_notes"], "Requires compute shader support; HDRP ready")
        self.assertIn("ocean123456", row["video_links"])
        self.assertEqual(row["enriched"], 1)

        # 4. FTS search must match the preserved enriched text
        fts_hits = conn.execute("SELECT id FROM assets_fts WHERE assets_fts MATCH 'simulation'").fetchall()
        self.assertTrue(any(r[0] == test_id for r in fts_hits))
        conn.close()

    def test_fts_migration_v2(self):
        conn = get_connection(self.db_path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(ver, 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
