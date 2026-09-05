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

        # Test Physical size and embedded HTML in maps
        sample_mesh = (
            "<p><strong>Texel density:</strong> 4871 px/m</p>"
            "<p><strong>Physical size:</strong> 2.53m x 1.66m x 0.18m</p>"
            "<p><strong>Mesh type:</strong> Open mesh</p>"
            "<p><strong>Maps:</strong> Displacement<em>(high tier only)</em> Cavity Gloss Specular Basecolor Roughness Normal AO Bump</p>"
        )
        specs_mesh = _parse_scan_specs_from_text(sample_mesh)
        self.assertEqual(specs_mesh["texel_density"], "4871 px/m")
        self.assertEqual(specs_mesh["scan_area"], "2.53m x 1.66m x 0.18m")
        self.assertIn("Displacement", specs_mesh["maps"])
        self.assertIn("Roughness", specs_mesh["maps"])
        self.assertNotIn("high", specs_mesh["maps"])

        # Test single spec without scan area (e.g. Castle Wall)
        sample_density_only = "<p><strong>Texel density:</strong> 5873 px/m</p>"
        specs_density = _parse_scan_specs_from_text(sample_density_only)
        self.assertEqual(specs_density["texel_density"], "5873 px/m")
        self.assertNotIn("scan_area", specs_density)

    def test_desktop_scan_specs_and_flow_layout(self):
        try:
            from src.desktop import _extract_scan_specs, FlowLayout
            from PySide6.QtWidgets import QApplication, QWidget, QLabel
            from PySide6.QtCore import QRect
        except (ImportError, OSError, RuntimeError) as e:
            self.skipTest(f"Qt GUI runtime not available: {e}")

        # Test noise filtering in maps extraction (e.g. Aloe Vera by Quixel Megascans)
        item = {
            "title": "Aloe Vera",
            "summary": "Aloe Vera by Quixel Megascans",
            "usage_notes": "Maps: Basecolor, Roughness, Specular, Bump, Gloss, Translucency, Displacement, AO, Normal, Opacity, Cavity",
        }
        specs = _extract_scan_specs(item)
        self.assertEqual(len(specs["maps"]), 11)
        self.assertIn("Basecolor", specs["maps"])
        self.assertIn("Cavity", specs["maps"])
        self.assertNotIn("Aloe", specs["maps"])
        self.assertNotIn("Vera", specs["maps"])
        self.assertNotIn("Quixel", specs["maps"])
        self.assertNotIn("Megascans", specs["maps"])

        # Test FlowLayout heightForWidth calculation
        app = QApplication.instance() or QApplication([])
        w = QWidget()
        flow = FlowLayout(w, margin=0, spacing=4)
        for m in specs["maps"]:
            lbl = QLabel(m)
            lbl.setFixedSize(60, 20)
            flow.addWidget(lbl)

        # At width 800, all 11 badges fit in one row (11 * 60 + 10 * 4 = 700 <= 800) -> height is 20
        h_wide = flow.heightForWidth(800)
        self.assertEqual(h_wide, 20)

        # At width 150, badges must wrap across multiple lines -> height is greater
        h_narrow = flow.heightForWidth(150)
        self.assertGreater(h_narrow, 20)

    def test_engine_compatibility_filtering(self):
        from src.mcp_server import _matches_engine
        
        quixel_asset = {"source": "quixel", "title": "Mossy Rock Scan", "formats": ["Unreal Engine", "FBX", "Textures"]}
        cosmos_asset = {"source": "cosmos", "title": "Medieval Italian Town", "formats": []}
        unity_asset = {"source": "unity", "title": "Amplify Shader Editor", "formats": []}
        fab_unreal_asset = {"source": "fab", "title": "Military Outpost UE5", "formats": []}
        fab_unity_asset = {"source": "fab", "title": "Modular Hospital (Unity Edition)", "formats": ["Unity"]}
        gumroad_ue_asset = {"source": "gumroad", "title": "Driveable Excavator (Unreal Engine)", "formats": []}
        gumroad_generic_asset = {"source": "gumroad", "title": "Fantasy Dagger Pack", "formats": []}

        # Unity project engine checks
        self.assertTrue(_matches_engine(quixel_asset, "unity"), "Quixel scans must be included in Unity")
        self.assertTrue(_matches_engine(cosmos_asset, "unity"), "Cosmos packs must be included in Unity")
        self.assertTrue(_matches_engine(unity_asset, "unity"), "Unity assets must be included in Unity")
        self.assertTrue(_matches_engine(fab_unreal_asset, "unity"), "Fab listings must be accessible in Unity (over-inclusion is recoverable)")
        self.assertTrue(_matches_engine(fab_unity_asset, "unity"), "Unity-declared Fab listing must match Unity")
        self.assertFalse(_matches_engine(gumroad_ue_asset, "unity"), "Unreal-only Gumroad pack must not match Unity")
        self.assertTrue(_matches_engine(gumroad_generic_asset, "unity"), "Generic Gumroad models must match Unity")

        # Unreal project engine checks
        self.assertTrue(_matches_engine(quixel_asset, "unreal"), "Quixel scans must be included in Unreal")
        self.assertTrue(_matches_engine(cosmos_asset, "unreal"), "Cosmos packs must be included in Unreal")
        self.assertTrue(_matches_engine(fab_unreal_asset, "unreal"), "Fab assets must match Unreal")
        self.assertFalse(_matches_engine(unity_asset, "unreal"), "Unity Asset Store package must not match Unreal")

        # Backwards compatibility: source passed as engine
        self.assertTrue(_matches_engine(quixel_asset, "quixel"))
        self.assertFalse(_matches_engine(unity_asset, "quixel"))

    def test_project_audit_gumroad_warning(self):
        from src.project_audit import compatibility_warning
        ue_pack = {"source": "gumroad", "title": "Military Boat (Unreal Engine)"}
        warnings = compatibility_warning(ue_pack, {"engine": "unity", "version": "Unity 6"})
        self.assertTrue(any("Unreal Engine" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
