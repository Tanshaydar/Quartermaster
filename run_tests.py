"""
Quartermaster Test Runner (Native unittest)
Runs all unit and security integration test suites with zero external test dependencies.
"""
import os
import sys
import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from src.unpacker import _sanitize_package_path, _safe_target, _strip_reason
from src.db import init_db, upsert_asset, mark_enriched, get_connection, search_assets
from src.semantic import hybrid_search
from src.server import app, ALLOWED_ORIGINS
from src.config import get_or_create_auth_token
import src.store_client as sc
from src.local_scan import _norm
from src.store_client import classify_asset
from src.stack_rules import validate_stack, list_recipes
from fastapi.testclient import TestClient


class TestUnpackerSandbox(unittest.TestCase):
    def test_sanitize_path_traversal(self):
        rel, parts = _sanitize_package_path("../../../etc/passwd")
        self.assertNotIn("..", parts)
        self.assertIn("passwd", parts)

    def test_sanitize_drive_letters(self):
        rel, parts = _sanitize_package_path("C:/Assets/Textures/Rock_01.png")
        self.assertEqual(parts, ["Textures", "Rock_01.png"])

        rel2, parts2 = _sanitize_package_path("Assets/Audio/Track:Reverb.wav")
        self.assertEqual(parts2, ["Audio", "Track:Reverb.wav"])

    def test_sanitize_null_bytes_and_control_chars(self):
        rel, parts = _sanitize_package_path("Assets/Models/Hero\x00\x01\x1fCharacter.fbx")
        self.assertNotIn("\x00", rel)
        self.assertNotIn("\x01", rel)
        self.assertEqual(parts, ["Models", "HeroCharacter.fbx"])

    def test_safe_target_enforcement(self):
        with tempfile.TemporaryDirectory() as tmp_proj:
            target = _safe_target(tmp_proj, ["Models", "Sword.fbx"])
            self.assertTrue(target.startswith(os.path.abspath(tmp_proj)))
            self.assertTrue(target.endswith(os.path.join("Assets", "Models", "Sword.fbx")))

    def test_strip_demo_rules(self):
        strip_dirs = {"demo", "demos", "samples", "sample", "example", "examples", "test"}
        strip_exts = {".unity", ".mp4", ".mov", ".avi"}

        self.assertIsNotNone(_strip_reason("Assets/Hero/Demo/Scene.unity", strip_dirs, strip_exts))
        self.assertIsNotNone(_strip_reason("Assets/Hero/Textures/Trailer.mp4", strip_dirs, strip_exts))
        self.assertIsNone(_strip_reason("Assets/Hero/Models/Hero.fbx", strip_dirs, strip_exts))


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


class TestConcurrencyAndSecurity(unittest.TestCase):
    def test_concurrent_searches(self):
        queries = ["medieval environment", "procedural foliage", "locomotion controller", "vfx magic", "sound effects"]
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(hybrid_search, q) for q in queries]
            results = [f.result() for f in futures]
        self.assertEqual(len(results), 5)
        for r in results:
            self.assertIn("search_mode", r)

    def test_csrf_and_origin_blocking(self):
        client = TestClient(app)
        tok = get_or_create_auth_token()

        # Subdomain attack: http://localhost:7890.attacker.com MUST be rejected with 403
        r_bad = client.post("/api/scan-local",
                            headers={"X-Quartermaster-Token": tok, "Origin": "http://localhost:7890.attacker.com"})
        self.assertEqual(r_bad.status_code, 403)

        # Exact valid origin MUST be accepted
        r_good = client.post("/api/scan-local",
                             headers={"X-Quartermaster-Token": tok, "Origin": "http://localhost:7890"})
        self.assertEqual(r_good.status_code, 200)

    def test_browser_profile_locking(self):
        with tempfile.TemporaryDirectory() as tmp_prof:
            lock = sc._acquire_profile_lock(tmp_prof, "unity")
            self.assertTrue(os.path.isabs(lock))
            
            with open(lock, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
                
            sc._release_profile_lock(tmp_prof)
            self.assertFalse(os.path.exists(lock))


class TestHelpersAndRules(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(_norm("Polygon - Fantasy Kingdom (v1.2)"), "polygonfantasykingdomv12")
        self.assertEqual(_norm("Mega_Pack_Sci-Fi"), "megapackscifi")

    def test_classify_asset(self):
        cls = classify_asset("Stylized Medieval Castle Environment Pack (URP / HDRP)")
        self.assertIn(cls["category"], ("3D Environments & Props", "3D Environments", "3D Models & Props", "General"))
        self.assertTrue(any("URP" in p or "HDRP" in p for p in cls["render_pipelines"]))

    def test_stack_rules(self):
        recipes = list_recipes()
        self.assertGreater(len(recipes), 0)
        self.assertIn("name", recipes[0])
        self.assertIn("owned_matches", recipes[0])

        val = validate_stack(["unity_123", "unity_456"])
        self.assertIn("verdict", val)
        self.assertEqual(val["verdict"], "ok")


if __name__ == "__main__":
    unittest.main()
