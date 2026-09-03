import os
import tempfile
import unittest
from src.local_scan import _norm
from src.store_client import classify_asset
from src.stack_rules import validate_stack, list_recipes
from src.db import init_db, upsert_asset


class TestHelpersAndRules(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_rules.db")
        init_db(self.db_path)

        # Seed mock assets for stack rule validation
        assets = [
            {"id": "mv_core", "source": "unity", "title": "MicroVerse - Core Collection", "category": "Terrain & Landscape"},
            {"id": "mv_roads", "source": "unity", "title": "MicroVerse - Roads", "category": "Terrain & Landscape"},
            {"id": "gaia_pro", "source": "unity", "title": "Gaia Pro 2021 - Terrain & Scene Generation", "category": "Terrain & Landscape"},
            {"id": "enviro_3", "source": "unity", "title": "Enviro 3 - Sky and Weather", "category": "Shaders & Rendering"},
            {"id": "unistorm", "source": "unity", "title": "UniStorm - Dynamic Weather", "category": "Shaders & Rendering"},
            {"id": "better_lit", "source": "unity", "title": "Better Lit Shader 2021", "category": "Shaders & Rendering"},
            {"id": "better_shaders", "source": "unity", "title": "Better Shaders - Standard/URP/HDRP", "category": "Shaders & Rendering"},
            {"id": "ms_trax", "source": "unity", "title": "MicroSplat - Trax", "category": "Shaders & Rendering"},
            {"id": "ms_core", "source": "unity", "title": "MicroSplat - Terrain Collection", "category": "Shaders & Rendering"},
        ]
        for a in assets:
            upsert_asset(a, db_path=self.db_path)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_norm(self):
        self.assertEqual(_norm("Polygon - Fantasy Kingdom (v1.2)"), "polygonfantasykingdomv12")
        self.assertEqual(_norm("Mega_Pack_Sci-Fi"), "megapackscifi")

    def test_classify_asset(self):
        cls = classify_asset("Stylized Medieval Castle Environment Pack (URP / HDRP)")
        self.assertIn(cls["category"], ("3D Environments & Props", "3D Environments", "3D Models & Props", "General"))
        self.assertTrue(any("URP" in p or "HDRP" in p for p in cls["render_pipelines"]))

    def test_stack_rules(self):
        recipes = list_recipes(db_path=self.db_path)
        self.assertGreater(len(recipes), 0)
        self.assertIn("name", recipes[0])
        self.assertIn("owned_matches", recipes[0])

        # 1. Empty / unknown IDs test
        val_empty = validate_stack(["unknown_1", "unknown_2"], db_path=self.db_path)
        self.assertEqual(val_empty["verdict"], "ok")
        self.assertEqual(len(val_empty["roles_detected"]), 0)

        # 2. Same-family detection (MicroVerse modules)
        val_mv = validate_stack(["mv_core", "mv_roads"], db_path=self.db_path)
        self.assertEqual(val_mv["verdict"], "ok")
        self.assertEqual(len(val_mv["conflicts"]), 0)
        self.assertEqual(len(val_mv["same_family_notes"]), 1)
        self.assertIn("Terrain generator", val_mv["roles_detected"])
        self.assertEqual(len(val_mv["roles_detected"]["Terrain generator"]), 2)

        # 3. Merged-pattern family detection (Better Lit + Better Shaders)
        val_lit = validate_stack(["better_lit", "better_shaders"], db_path=self.db_path)
        self.assertEqual(val_lit["verdict"], "ok")
        self.assertEqual(len(val_lit["conflicts"]), 0)
        self.assertEqual(len(val_lit["same_family_notes"]), 1)

        # 4. Genuine exclusive conflict (MicroVerse vs Gaia)
        val_terrain_conflict = validate_stack(["mv_core", "gaia_pro"], db_path=self.db_path)
        self.assertEqual(val_terrain_conflict["verdict"], "issues-found")
        self.assertEqual(len(val_terrain_conflict["conflicts"]), 1)
        self.assertEqual(val_terrain_conflict["conflicts"][0]["role"], "Terrain generator")

        # 5. Genuine weather conflict (Enviro vs UniStorm)
        val_weather_conflict = validate_stack(["enviro_3", "unistorm"], db_path=self.db_path)
        self.assertEqual(val_weather_conflict["verdict"], "issues-found")
        self.assertEqual(len(val_weather_conflict["conflicts"]), 1)
        self.assertEqual(val_weather_conflict["conflicts"][0]["role"], "Weather / sky system")

        # 6. Missing prerequisite (MicroSplat Trax alone)
        val_prereq_missing = validate_stack(["ms_trax"], db_path=self.db_path)
        self.assertEqual(val_prereq_missing["verdict"], "issues-found")
        self.assertEqual(len(val_prereq_missing["missing_prerequisites"]), 1)
        self.assertEqual(val_prereq_missing["missing_prerequisites"][0]["requires"], "microsplat")

        # 7. Satisfied prerequisite (MicroSplat Trax + MicroSplat Core)
        val_prereq_satisfied = validate_stack(["ms_trax", "ms_core"], db_path=self.db_path)
        self.assertEqual(val_prereq_satisfied["verdict"], "ok")
        self.assertEqual(len(val_prereq_satisfied["missing_prerequisites"]), 0)

    def test_update_checker_version_comparison(self):
        def is_newer(latest_tag: str, current_ver: str) -> bool:
            tag = latest_tag.lstrip("v").strip()
            cur_parts = [int(p) for p in current_ver.split(".") if p.isdigit()]
            lat_parts = [int(p) for p in tag.split(".") if p.isdigit()]
            return lat_parts > cur_parts

        self.assertTrue(is_newer("v1.1.3", "1.1.2"))
        self.assertTrue(is_newer("1.2.0", "1.1.2"))
        self.assertTrue(is_newer("v2.0.0", "1.1.2"))
        self.assertFalse(is_newer("v1.1.2", "1.1.2"))
        self.assertFalse(is_newer("v1.1.1", "1.1.2"))
        self.assertFalse(is_newer("v1.0.0", "1.1.2"))

    def test_parse_scan_specs(self):
        from src.local_scan import _parse_scan_specs_from_text
        sample = (
            "<p><strong>Texel density:</strong> 8192 px/m</p>"
            "<p><strong>Scan Area:</strong> 1x1 m</p>"
            "<p><strong>Maps:</strong> Basecolor Displacement Cavity AO Specular Roughness Gloss Normal Bump</p>"
        )
        specs = _parse_scan_specs_from_text(sample)
        self.assertEqual(specs["texel_density"], "8192 px/m")
        self.assertEqual(specs["scan_area"], "1x1 m")
        self.assertIn("Displacement", specs["maps"])
        self.assertIn("Roughness", specs["maps"])


if __name__ == "__main__":
    unittest.main()
