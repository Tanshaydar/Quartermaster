"""
VaultMCP one-click MCP registration.

Registers the VaultMCP MCP server into supported AI clients:

  python -m src.register --all           # every detected client
  python -m src.register claude cursor   # specific clients
  python -m src.register --all --dry-run # show what would change
  python -m src.register --remove --all  # unregister

Behavior:
  - merges into existing JSON config (never clobbers unrelated keys)
  - writes a .quartermaster-backup next to the file before first modification
  - clients whose config file doesn't exist yet are created (except where
    the parent app clearly isn't installed)
"""
import json
import os
import shutil
import sys
from typing import Any, Dict, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", "")


def _server_entry() -> Dict[str, Any]:
    return {
        "command": sys.executable,
        "args": ["-m", "src.mcp_server"],
        "cwd": ROOT_DIR,
    }


# candidate config paths per client (first existing wins; "create_ok" means we
# may create the file even if it doesn't exist yet)
CLIENTS: Dict[str, Dict[str, Any]] = {
    "claude": {
        "name": "Claude Desktop",
        "candidates": [
            os.path.join(APPDATA, "Claude", "claude_desktop_config.json"),
            os.path.join(HOME, ".claude", "claude_desktop_config.json"),
        ],
        "key": "mcpServers",
        "create_ok": False,      # only touch if Claude Desktop is installed
        "install_hint": "config appears when Claude Desktop is installed (%APPDATA%\\Claude)",
    },
    "cursor": {
        "name": "Cursor",
        "candidates": [
            os.path.join(HOME, ".cursor", "mcp.json"),
        ],
        "key": "mcpServers",
        "create_ok": True,
        "install_hint": "~/.cursor/mcp.json",
    },
    "windsurf": {
        "name": "Windsurf",
        "candidates": [
            os.path.join(HOME, ".codeium", "windsurf", "mcp_config.json"),
        ],
        "key": "mcpServers",
        "create_ok": True,
        "install_hint": "~/.codeium/windsurf/mcp_config.json",
    },
    "antigravity": {
        "name": "Antigravity",
        "candidates": [
            os.path.join(HOME, ".gemini", "antigravity", "mcp_config.json"),
            os.path.join(HOME, ".gemini", "antigravity", "mcp", "mcp_config.json"),
        ],
        "key": "mcpServers",
        "create_ok": False,
        "install_hint": "~/.gemini/antigravity/ (config name may differ between versions)",
    },
}


def _find_config(client: str) -> Optional[str]:
    for p in CLIENTS[client]["candidates"]:
        if os.path.isfile(p):
            return p
    return None


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def register_client(client: str, dry_run: bool = False, remove: bool = False) -> str:
    """Returns a human-readable status line."""
    spec = CLIENTS[client]
    key = spec["key"]
    path = _find_config(client)

    if not path:
        if spec["create_ok"]:
            path = spec["candidates"][0]
        else:
            return f"[skip] {spec['name']}: no config found ({spec['install_hint']})"

    cfg = _read_json(path) if os.path.exists(path) else {}
    servers = cfg.setdefault(key, {})

    already = "quartermaster" in servers
    if remove:
        if not already:
            return f"[skip] {spec['name']}: not registered"
        if not dry_run:
            servers.pop("quartermaster")
            _write_json(path, cfg)
        return f"[{'dry-run' if dry_run else 'ok'}] {spec['name']}: removed ({path})"

    if already and servers["quartermaster"] == _server_entry():
        return f"[skip] {spec['name']}: already up-to-date ({path})"

    if not dry_run:
        if os.path.exists(path) and not os.path.exists(path + ".quartermaster-backup"):
            shutil.copy2(path, path + ".quartermaster-backup")
        servers["quartermaster"] = _server_entry()
        _write_json(path, cfg)
    verb = "would register" if dry_run else "registered"
    note = " (updated)" if already else ""
    return f"[{'dry-run' if dry_run else 'ok'}] {spec['name']}: {verb}{note} ({path})"


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    remove = "--remove" in argv
    dry_run = "--dry-run" in argv
    targets = args if args else []
    if "--all" in argv or not targets:
        targets = list(CLIENTS.keys())

    print(f"VaultMCP server: python -m src.mcp_server @ {ROOT_DIR}")
    for t in targets:
        t = t.lower().strip("-")
        if t not in CLIENTS:
            print(f"[error] unknown client '{t}'. Known: {', '.join(CLIENTS)}")
            continue
        print(register_client(t, dry_run=dry_run, remove=remove))


if __name__ == "__main__":
    main(sys.argv[1:])
