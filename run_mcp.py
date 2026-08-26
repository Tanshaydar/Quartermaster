import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if "--register" in sys.argv or "--unregister" in sys.argv:
    from src import register
    reg_argv = [a for a in sys.argv[1:] if a != "--register"]
    if "--unregister" in sys.argv:
        reg_argv.append("--remove")
    register.main(reg_argv)
    sys.exit(0)

from src.mcp_server import main

if __name__ == "__main__":
    main()
