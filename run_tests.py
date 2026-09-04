"""
Quartermaster Test Runner (Native unittest)
Runs all unit and security integration test suites from the tests/ directory.
Zero external test dependencies required.
"""
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(BASE_DIR, "tests"), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(not result.wasSuccessful())
