import os
import unittest
import tempfile
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from src.server import app, ALLOWED_ORIGINS
from src.config import get_or_create_auth_token
from src.semantic import hybrid_search
import src.store_client as sc


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

        with patch("src.server.local_scan.scan_all", return_value={"files_scanned": {}, "matched_to_library": 0}):
            # 1. Subdomain attack: http://localhost:7890.attacker.com MUST be rejected with 403
            r_bad = client.post("/api/scan-local",
                                headers={"X-Quartermaster-Token": tok, "Origin": "http://localhost:7890.attacker.com"})
            self.assertEqual(r_bad.status_code, 403)

            # 2. Exact valid origin MUST be accepted
            r_good = client.post("/api/scan-local",
                                 headers={"X-Quartermaster-Token": tok, "Origin": "http://localhost:7890"})
            self.assertEqual(r_good.status_code, 200)

            # 3. Headerless no-referrer subresource attack (no Origin, no Referer, no Token) on /api/ MUST be rejected with 403
            r_no_ref = client.get("/api/assets?query=test")
            self.assertEqual(r_no_ref.status_code, 403)

            # 4. Headerless with valid token (e.g. Unity bridge, curl) MUST be accepted with 200
            r_tok = client.get("/api/assets?query=test", headers={"X-Quartermaster-Token": tok})
            self.assertEqual(r_tok.status_code, 200)

            # 5. Same-origin Referer (Web UI) MUST be accepted with 200
            r_web = client.get("/api/assets?query=test", headers={"Referer": "http://localhost:7890/"})
            self.assertEqual(r_web.status_code, 200)

    def test_browser_profile_locking(self):
        with tempfile.TemporaryDirectory() as tmp_prof:
            lock = sc._acquire_profile_lock(tmp_prof, "unity")
            self.assertTrue(os.path.isabs(lock))
            
            with open(lock, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
                
            sc._release_profile_lock(tmp_prof)
            self.assertFalse(os.path.exists(lock))


if __name__ == "__main__":
    unittest.main()
