# VaultMCP

Your personal game-asset vault: ingest everything you own from the **Unity Asset
Store** and **Epic Games Fab/Unreal Marketplace**, cache metadata, keywords,
images and video links locally, then let **any AI agent** (Claude, Antigravity,
pi, …) sweep your vault via MCP and answer questions like:

> *"I have this game in mind — sweep my asset list and tell me what can be used
> from which package."*

## Components

| Piece | What it does |
|---|---|
| `src/db.py` | SQLite + FTS5 full-text search with weighted ranking |
| `src/ingest.py` | CSV / JSON import (Unity + Fab exports, auto-detected) |
| `src/desktop.py` | **Native desktop app** (PySide6): search, detail view, sync, tray + global hotkey |
| `src/server.py` | Optional browser UI + REST API + image proxy cache |
| `src/local_scan.py` | Scans Unity/Fab disk caches, tags assets as downloaded vs cloud |
| `src/store_client.py` | Interactive store login, library refresh, metadata enrichment |
| `src/mcp_server.py` | MCP server (stdio) exposing the vault to AI agents |
| `web/` | Browser-mode UI (same features as the desktop app) |

## Quick start

```bash
pip install -r requirements.txt

# 1. Ingest your CSV exports (drop them in data/seed/ first)
python -m src.ingest

# 2a. Launch the native desktop app (recommended)
python -m src.desktop

# 2b. …or the browser UI
python -m src.server          # -> http://localhost:7890
```

The desktop app runs a disk-cache scan on startup, tags everything already
downloaded as **⚡ Local**, and registers a global hotkey **Win+Alt+V** to
show/hide the window (Spotlight-style). Closing the window minimizes to the
system tray; quit from the tray menu or Ctrl+Q.

### Syncing your store libraries (per-user login)

1. In the UI press **⚙ Sync** → **Login with Unity / Login with Epic-Fab**.
   A browser window opens; sign in with **your own account** (2FA works fine).
   The session is saved into `profiles/<provider>/` on your machine.
2. Press **Fetch … library**. The app opens the store's library page with your
   saved session, intercepts the asset listings, and upserts them into the vault.
3. Press **Enrich batch** (repeat as desired) to pull cover images, screenshots,
   and video links (YouTube/store trailers — links only, never downloaded).

Images are cached to disk through the `/api/image` proxy (toggle in
`config.json`: `media_cache_enabled`).

### Registering the MCP server with an AI client

See `mcp_config.json`. For Claude Desktop / Antigravity / pi, point the MCP
server at:

```
command: python
args:    ["-m", "src.mcp_server"]
cwd:     D:\Projects\PERSONAL\VaultMCP
```

Tools exposed:

- `search_owned_assets(query, engine, pipeline, category, limit)`
- `get_asset_details(asset_id)`
- `list_asset_categories()`
- `get_stack_recommendations(problem_description)` ← *"sweep my vault for my game idea"*
- `get_vault_stats()`

## CLI

```bash
python -m src.ingest                                  # ingest all CSVs in data/seed/
python -m src.ingest --csv path.csv --source unity    # specific file
python -m src.store_client login <unity|fab>          # interactive login
python -m src.store_client fetch <unity|fab>          # refresh library
python -m src.store_client enrich [limit]             # enrich metadata batch
```

## Notes

- Everything runs locally; no cloud dependency.
- Store sessions are stored only in local browser profiles on your machine.
- The library fetcher intercepts the stores' own page traffic rather than
  hardcoding private API endpoints, so it survives endpoint churn.
