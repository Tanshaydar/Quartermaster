import json
import os
import sqlite3
import tempfile
import unittest
import numpy as np

from src.db import init_db, upsert_asset, get_connection
from src.vision import _ensure_schema, invalidate_vision_cache
from src.semantic import hybrid_search, invalidate_vector_cache


class TestVisionAndHybridSearch(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_assets.db")
        init_db(self.db_path)

    def tearDown(self):
        invalidate_vision_cache()
        invalidate_vector_cache()
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_fts_migration_v3_vision_tags(self):
        conn = get_connection(self.db_path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(ver, 3)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(assets)")]
        self.assertIn("vision_tags", cols)

        # Verify assets_fts indexes vision_tags
        upsert_asset({
            "id": "asset_test_1",
            "source": "unity",
            "title": "Generic Mystery Pack",
            "publisher": "Unknown",
            "category": "3D Environments",
            "vision_tags": ["gothic cathedral interior", "stone gargoyles"],
        }, db_path=self.db_path)

        # Direct FTS query on vision_tag keyword
        fts_hits = conn.execute("""
            SELECT id FROM assets_fts WHERE assets_fts MATCH 'cathedral'
        """).fetchall()
        self.assertEqual(len(fts_hits), 1)
        self.assertEqual(fts_hits[0][0], "asset_test_1")
        conn.close()

    def test_3way_hybrid_search_fusion(self):
        # Insert test asset
        upsert_asset({
            "id": "asset_church",
            "source": "unity",
            "title": "Ancient Village Pack",
            "summary": "Medieval props and foliage.",
            "category": "3D Environments",
            "vision_tags": ["gothic church"],
        }, db_path=self.db_path)

        upsert_asset({
            "id": "asset_scifi",
            "source": "fab",
            "title": "Modular Space Station",
            "summary": "High tech corridors and blast doors.",
            "category": "3D Environments",
            "vision_tags": ["sci-fi corridor"],
        }, db_path=self.db_path)

        # Mock image vector in image_vectors
        conn = get_connection(self.db_path)
        _ensure_schema(conn)

        # 512-dim mock normalized vector
        v = np.zeros(512, dtype=np.float32)
        v[0] = 1.0
        conn.execute("""
            INSERT INTO image_vectors (id, asset_id, image_url, vector)
            VALUES (?,?,?,?)
        """, ("img_1", "asset_church", "https://media.fab.com/church.png", v.tobytes()))
        conn.commit()
        conn.close()

        # Run hybrid search
        res = hybrid_search("gothic church", limit=10, db_path=self.db_path)
        self.assertGreater(res["count"], 0)
        top = res["results"][0]
        self.assertEqual(top["id"], "asset_church")
        self.assertIn("match", top)
        self.assertIn("relevance", top)

    def test_fts_sync_vision_tags_migration_v4(self):
        # Insert an asset without vision tags
        upsert_asset({
            "id": "asset_drift",
            "source": "gumroad",
            "title": "Drifting Sci-Fi Drone",
            "publisher": "Vendor",
            "category": "3D Models",
            "vision_tags": [],
        }, db_path=self.db_path)

        conn = get_connection(self.db_path)
        # Simulate out-of-band vision tag write (e.g. from vision.py concept mining)
        conn.execute("UPDATE assets SET vision_tags = ? WHERE id = ?",
                     (json.dumps(["cyberpunk hovering drone"]), "asset_drift"))
        conn.commit()

        # FTS still has empty vision_tags before sync
        hit_before = conn.execute("SELECT id FROM assets_fts WHERE assets_fts MATCH 'hovering'").fetchall()
        self.assertEqual(len(hit_before), 0)

        # Reset user_version to 3 to trigger v4 migration on init_db
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        # Re-initialize to trigger migration v4
        init_db(self.db_path)

        conn = get_connection(self.db_path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(ver, 4)
        hit_after = conn.execute("SELECT id FROM assets_fts WHERE assets_fts MATCH 'hovering'").fetchall()
        self.assertEqual(len(hit_after), 1)
        self.assertEqual(hit_after[0][0], "asset_drift")
        conn.close()


if __name__ == "__main__":
    unittest.main()
