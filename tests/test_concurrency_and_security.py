import os
import tempfile
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from src.server import app
from src.config import get_or_create_auth_token
from src.server import ALLOWED_ORIGINS
from src.semantic import hybrid_search
import src.store_client as sc


def test_concurrent_searches():
    queries = ["medieval environment", "procedural foliage", "locomotion controller", "vfx magic", "sound effects"]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(hybrid_search, q) for q in queries]
        results = [f.result() for f in futures]
    assert len(results) == 5
    for r in results:
        assert "search_mode" in r


def test_csrf_and_origin_blocking():
    client = TestClient(app)
    tok = get_or_create_auth_token()

    # Subdomain attack: http://localhost:7890.attacker.com MUST be rejected with 403
    r_bad = client.post("/api/scan-local",
                        headers={"X-Quartermaster-Token": tok, "Origin": "http://localhost:7890.attacker.com"})
    assert r_bad.status_code == 403

    # Exact valid origin MUST be accepted
    r_good = client.post("/api/scan-local",
                         headers={"X-Quartermaster-Token": tok, "Origin": "http://localhost:7890"})
    assert r_good.status_code == 200


def test_browser_profile_locking():
    with tempfile.TemporaryDirectory() as tmp_prof:
        lock = sc._acquire_profile_lock(tmp_prof, "unity")
        assert os.path.isabs(lock)
        
        # Write own pid
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
            
        # Releasing clears lock
        sc._release_profile_lock(tmp_prof)
        assert not os.path.exists(lock)
