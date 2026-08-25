import unittest
from src.local_scan import _norm
from src.store_client import classify_asset
from src.stack_rules import validate_stack, list_recipes


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
