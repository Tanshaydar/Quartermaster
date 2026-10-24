import asyncio
import unittest

from src import mcp_server


def _annotations():
    """Each tool's annotations as clients receive them: camelCase, with unset hints left out."""
    return {t.name: t.annotations.model_dump(by_alias=True, exclude_none=True) if t.annotations else None
            for t in asyncio.run(mcp_server.mcp.list_tools())}


class TestToolAnnotations(unittest.TestCase):
    """No tool declared annotations. Directories that list each tool's reach from them took every
    tool for read-only, import_asset_to_project included, though it writes into the project, while
    clients following the spec's defaults had to assume any tool could write and reach the network.
    Each tool now declares what it does."""

    def test_every_tool_declares_annotations(self):
        self.assertEqual([name for name, ann in _annotations().items() if ann is None], [])

    def test_only_the_import_writes(self):
        writes = {name for name, ann in _annotations().items() if not (ann or {}).get("readOnlyHint")}
        self.assertEqual(writes, {"import_asset_to_project"})

    def test_import_declares_it_overwrites_files(self):
        self.assertEqual(_annotations()["import_asset_to_project"], {
            "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})

    def test_no_tool_reaches_outside_the_machine(self):
        # openWorldHint defaults to true when it isn't sent
        open_world = {name for name, ann in _annotations().items() if (ann or {}).get("openWorldHint") is not False}
        self.assertEqual(open_world, set())


if __name__ == "__main__":
    unittest.main()
