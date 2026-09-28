import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import mcp_server
from src.server import app

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Runs in a fresh interpreter, since this one has imported both servers already. DB_PATH is
# rebound to a scratch file before src.db copies it, so a regression writes there instead of
# the vault, and every sqlite3.connect() the imports make is recorded.
IMPORT_SERVERS = """
import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from src import config
config.DB_PATH = sys.argv[2]
opened, connect = [], sqlite3.connect
def spy(database, *args, **kwargs):
    opened.append(str(database))
    return connect(database, *args, **kwargs)
sqlite3.connect = spy
import src.server, src.mcp_server, src.db
print(json.dumps({"db_path": src.db.DB_PATH, "opened": opened}))
"""


class TestDbInitAtStartup(unittest.TestCase):
    """src.server and src.mcp_server ran init_db() at import time, so every test module importing
    them migrated and pruned the checkout's data/assets.db, which is the user's real vault when the
    suite runs from the main checkout. They now initialize it when the app starts instead."""

    def test_importing_the_servers_opens_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "data", "assets.db")
            proc = subprocess.run([sys.executable, "-c", IMPORT_SERVERS, ROOT_DIR, db_path],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout.splitlines()[-1])
            self.assertEqual(out["db_path"], db_path)   # the redirect took effect
            self.assertEqual(out["opened"], [])
            self.assertFalse(os.path.exists(db_path))

    def test_web_server_initializes_the_database_on_startup(self):
        with patch("src.server.init_db") as init_db:
            with TestClient(app):   # runs the lifespan startup, before any request is served
                init_db.assert_called_once_with()

    def test_mcp_server_initializes_the_database_before_serving(self):
        order = []
        with patch("src.mcp_server.init_db", lambda: order.append("init_db")), \
                patch.object(mcp_server.mcp, "run", lambda **_: order.append("run")):
            mcp_server.main()
        self.assertEqual(order, ["init_db", "run"])


if __name__ == "__main__":
    unittest.main()
