import unittest
from src.store_client import _extract_store_url, _extract_media_images, _extract_publisher
from src.config import ALLOWED_IMAGE_DOMAINS
from src.desktop import _source_icon, _format_engine_name
from src.mcp_server import _slim


class TestStoreClientParsers(unittest.TestCase):
    def test_extract_store_url_gumroad(self):
        # Permalinks / slugs
        self.assertEqual(
            _extract_store_url({"permalink": "modular-scifi-corridor"}, "gumroad"),
            "https://app.gumroad.com/library?item=modular-scifi-corridor"
        )
        # Relative URLs
        self.assertEqual(
            _extract_store_url({"url": "/l/cyberpunk-pack"}, "gumroad"),
            "https://app.gumroad.com/l/cyberpunk-pack"
        )
        # IDs
        self.assertEqual(
            _extract_store_url({"id": "item_abc123"}, "gumroad"),
            "https://app.gumroad.com/library?item=item_abc123"
        )
        # Absolute URL passthrough
        self.assertEqual(
            _extract_store_url({"url": "https://author.gumroad.com/l/custom"}, "gumroad"),
            "https://author.gumroad.com/l/custom"
        )

    def test_extract_store_url_cosmos(self):
        # Slugs
        self.assertEqual(
            _extract_store_url({"slug": "post-apocalyptic-abandoned-city"}, "cosmos"),
            "https://cosmos.leartesstudios.com/product/post-apocalyptic-abandoned-city"
        )
        # Relative URLs
        self.assertEqual(
            _extract_store_url({"url": "/product/stylized-town"}, "cosmos"),
            "https://cosmos.leartesstudios.com/product/stylized-town"
        )
        # IDs
        self.assertEqual(
            _extract_store_url({"id": "cos_9999"}, "cosmos"),
            "https://cosmos.leartesstudios.com/product/cos_9999"
        )

    def test_extract_store_url_unity_and_fab(self):
        # Regression checks
        self.assertEqual(
            _extract_store_url({"slug": "gaia-pro-2021"}, "unity"),
            "https://assetstore.unity.com/packages/gaia-pro-2021"
        )
        self.assertEqual(
            _extract_store_url({"listingSlug": "megascans-trees"}, "fab"),
            "https://www.fab.com/listings/megascans-trees"
        )

    def test_extract_media_images(self):
        # Gumroad cover image and screenshots
        g_item = {
            "cover_image": "https://gumroadcdn.com/res/cover.png",
            "images": [
                "https://gumroadcdn.com/res/screen1.png",
                "https://gumroadcdn.com/res/screen2.png"
            ]
        }
        cover, gallery = _extract_media_images(g_item)
        self.assertEqual(cover, "https://gumroadcdn.com/res/cover.png")
        self.assertIn("https://gumroadcdn.com/res/screen1.png", gallery)
        self.assertIn("https://gumroadcdn.com/res/screen2.png", gallery)

        # Cosmos preview and images list
        c_item = {
            "preview_url": "https://cosmos.leartesstudios.com/preview.webp",
            "images": [
                {"url": "https://cosmos.leartesstudios.com/screen1.webp"},
                {"url": "https://cosmos.leartesstudios.com/screen2.webp"}
            ]
        }
        cover_c, gallery_c = _extract_media_images(c_item)
        self.assertEqual(cover_c, "https://cosmos.leartesstudios.com/preview.webp")
        self.assertEqual(len(gallery_c), 3)

    def test_extract_publisher(self):
        # Gumroad creator dict or string
        self.assertEqual(_extract_publisher({"creator": {"name": "Level Designer"}}), "Level Designer")
        self.assertEqual(_extract_publisher({"creator": "IndieDev"}), "IndieDev")

        # Cosmos publisher
        self.assertEqual(_extract_publisher({"publisher": {"displayName": "Leartes Studios"}}), "Leartes Studios")
        self.assertEqual(_extract_publisher({"sellerName": "Cyberpunk Team"}), "Cyberpunk Team")

    def test_allowed_image_domains(self):
        self.assertIn("gumroad.com", ALLOWED_IMAGE_DOMAINS)
        self.assertIn(".gumroad.com", ALLOWED_IMAGE_DOMAINS)
        self.assertIn("gumroadcdn.com", ALLOWED_IMAGE_DOMAINS)
        self.assertIn(".gumroadcdn.com", ALLOWED_IMAGE_DOMAINS)
        self.assertIn("cosmos.leartesstudios.com", ALLOWED_IMAGE_DOMAINS)
        self.assertIn("leartesstudios.com", ALLOWED_IMAGE_DOMAINS)
        self.assertIn(".leartesstudios.com", ALLOWED_IMAGE_DOMAINS)

    def test_desktop_source_helpers(self):
        self.assertEqual(_source_icon("unity"), "📦")
        self.assertEqual(_source_icon("fab"), "🌿")
        self.assertEqual(_source_icon("quixel"), "🌿")
        self.assertEqual(_source_icon("gumroad"), "🎨")
        self.assertEqual(_source_icon("cosmos"), "🪐")

        self.assertEqual(_format_engine_name("unity"), "Unity Asset Store")
        self.assertEqual(_format_engine_name("fab"), "Fab (Unreal)")
        self.assertEqual(_format_engine_name("quixel"), "Quixel Megascans")
        self.assertEqual(_format_engine_name("gumroad"), "Gumroad")
        self.assertEqual(_format_engine_name("cosmos"), "Leartes Cosmos")

    def test_mcp_ownership_classification(self):
        self.assertEqual(_slim({"id": "g1", "title": "G", "source": "gumroad"})["ownership"], "vault_owned")
        self.assertEqual(_slim({"id": "c1", "title": "C", "source": "cosmos"})["ownership"], "vault_owned")
        self.assertEqual(_slim({"id": "u1", "title": "U", "source": "unity"})["ownership"], "vault_owned")
        self.assertEqual(_slim({"id": "f1", "title": "F", "source": "fab"})["ownership"], "vault_owned")
        self.assertEqual(_slim({"id": "q1", "title": "Q", "source": "quixel"})["ownership"], "catalog_grant")


if __name__ == "__main__":
    unittest.main()
