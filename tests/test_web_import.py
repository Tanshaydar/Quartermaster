import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.config import get_or_create_auth_token
from src.server import app


class TestApiImport(unittest.TestCase):
    """The Unity editor bridge only refreshes the AssetDatabase, reports the import and offers
    'Add to Scene' when /api/import answers status == "ok", which the endpoint never sent, so every
    successful import looked like nothing happened. Failures must stay non-2xx: the bridge reports
    those from the HTTP error instead."""

    def setUp(self):
        self.client = TestClient(app)
        self.headers = {"X-Quartermaster-Token": get_or_create_auth_token()}
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _post(self, **form):
        return self.client.post("/api/import", headers=self.headers, data=form)

    def test_success_reports_status_ok_with_the_fields_the_bridge_reads(self):
        prefab = os.path.join(self.project, "Assets", "Knight", "KnightArmored.prefab")
        os.makedirs(os.path.dirname(prefab))
        open(prefab, "w").close()
        unpacked = {"written": 12, "skipped": 0, "stripped": 3, "stripped_mb": 1.5,
                    "project": self.project, "package": "knight.unitypackage",
                    "warnings": ["render pipeline mismatch"],
                    "target": {"engine": "unity", "version": "2022.3", "pipeline": "builtin"},
                    "title": "Knight Character Pack", "asset_id": "unity:42"}
        expected = {"status": "ok", **unpacked, "prefabs": [prefab]}

        with patch("src.server.unpacker.import_asset_to_project", return_value=unpacked) as imp:
            r = self._post(asset_id="unity:42", project_dir=self.project, strip_demos="false")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), expected)
        imp.assert_called_once_with("unity:42", self.project, strip_demos=False)

    def test_failure_is_an_http_error_not_a_200(self):
        with patch("src.server.unpacker.import_asset_to_project",
                   side_effect=ValueError("Asset 'Knight Character Pack' is not downloaded locally.")):
            r = self._post(asset_id="unity:42", project_dir=self.project)

        self.assertEqual(r.status_code, 400)
        self.assertIn("not downloaded locally", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
