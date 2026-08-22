# VaultMCP

**Your game-asset vault, queryable by AI.**

VaultMCP indexes everything you own from the **Unity Asset Store** and **Fab
(Unreal Marketplace)** into one fast local search engine — then exposes it to
AI agents (Claude Desktop, Cursor, Windsurf, Antigravity, …) over
[MCP](https://modelcontextprotocol.io) so they can answer questions like:

> *"I want to build a third-person fantasy game in a forest village with magic
> combat. Sweep my assets — what can I use from which package?"*

> *"Find me water shaders that work in my HDRP project and are already
> downloaded."*

> *"Import the road props pack into D:/Projects/MyGame."*

Everything runs **100% locally**. No cloud service, no account with us, no
telemetry.

---

## Why

If you've bought assets for more than a year, you own hundreds of packages you
can't remember. So you re-buy what you already have, or ship with your third
favorite option because you forgot the first one existed.

VaultMCP fixes the *remembering* part:

| Without VaultMCP | With VaultMCP |
|---|---|
| Scroll the Asset Store purchase list manually | Ask an agent in natural language |
| Re-buy assets you already own | Agent checks your vault first |
| Import URP-only shader into HDRP project | Automatic pipeline-mismatch warning |
| Download → find zip → drag into editor | One tool call unpacks into `Assets/` |

## Features

- **1,400+ assets searched in milliseconds** — SQLite FTS5 with weighted ranking
- **Hybrid semantic search** — offline CPU embeddings (`bge-small-en-v1.5` via
  fastembed) fused with keyword search, so *"spooky abandoned industrial site"*
  finds *Warehouse – Abandoned Factory District* even though no tag says "spooky"
- **Local disk scanner** — tags every asset as ⚡ downloaded or ☁ cloud-only by
  scanning the Unity Asset Store cache and Fab vault automatically
- **Direct project import** — unpacks `.unitypackage` files straight into a
  project's `Assets/`, stripping demo scenes/docs (configurable), with
  path-safety guarantees
- **Stack intelligence** — role-based conflict linting ("two weather systems",
  "MicroSplat module without core"), plus curated *asset recipes* resolved
  against your own library (`data/recipes.json` is user-editable)
- **In-editor Unity window** — drop `editor_bridge/VaultMCP-Bridge.unitypackage`
  into any project to search/import from inside Unity (`Window > VaultMCP`)
- **Project auditor** — reads a target project's engine, version, and render
  pipeline; warns before incompatible imports (*"asset supports HDRP but
  project uses URP"*)
- **Per-user store login** — sign in with your own Unity/Epic account once;
  sessions stay on your machine
- **Native desktop app** — PySide6, dark mode, global hotkey (**Win+Alt+V**),
  system tray
- **Image caching & video links** — covers/screenshots cached to disk; YouTube
  trailers stored as links, never downloaded

## Quick start

Requirements: **Python 3.11+** (3.12–3.14 tested on Windows).

```bash
git clone <repo-url> VaultMCP
cd VaultMCP
pip install -r requirements.txt

# 1. Seed your library from CSV exports (see below), then:
python -m src.ingest

# 2. Launch the desktop app
python -m src.desktop
```

### Getting your library in

Any of these work — combine freely:

| Method | How |
|---|---|
| CSV export | Drop your Unity/Fab export CSVs into `data/seed/`, run `python -m src.ingest` |
| Disk scan | Runs automatically at app startup; also available as ⚡ button |
| Store login | ⚙ Sync → *Login with Unity / Fab* → sign in once in the browser window → *Fetch* |
| Loose folders | Packages discovered on disk that aren't in your library get adopted automatically |

### Connect your AI agent (one command)

```bash
python -m src.register --all          # Claude Desktop, Cursor, Windsurf, Antigravity
```

Existing MCP servers in those configs are preserved; a backup is written next
to each file. Undo anytime with `--remove`.

## What agents can do (MCP tools)

| Tool | Purpose |
|---|---|
| `search_owned_assets(query, engine, pipeline, category, local_only)` | Hybrid keyword + semantic search across your vault |
| `get_asset_details(asset_id)` | Full metadata: summary, usage notes, gallery, video links |
| `list_asset_categories()` | Category breakdown of what you own |
| `get_stack_recommendations(problem_description)` | *"I'm building X"* → matched owned packages per aspect |
| `audit_project(project_dir)` | Detects engine/version/pipeline of a game project |
| `validate_stack(asset_ids)` | Lints a planned stack: role conflicts, missing prerequisites |
| `list_stack_recipes()` | Curated production stacks resolved to assets **you own** |
| `import_asset_to_project(asset_id, project_dir)` | Unpack a downloaded `.unitypackage` into a Unity project, demo content stripped, compatibility warnings included |

Example agent flow: audit project → search vault (`local_only=true`) → warn on
pipeline mismatch → unpack into `Assets/`. No manual step anywhere.

## CLI reference

```bash
python -m src.desktop                     # native app (recommended)
python -m src.server                      # browser UI at http://localhost:7890
python -m src.mcp_server                  # MCP server (usually launched BY the agent)
python -m src.ingest                      # ingest CSVs from data/seed/
python -m src.semantic build              # rebuild embedding index after big imports
python -m src.store_client login unity    # interactive store login
python -m src.store_client fetch fab      # refresh Fab library headlessly
python -m src.local_scan                  # rescan disk caches
python -m src.register --all --dry-run    # preview agent registration changes
python -m src.project_audit <project_dir> # inspect a game project
```

## Configuration

Optional `config.json` in the project root (all keys optional):

```jsonc
{
  "server_port": 7890,
  "media_cache_enabled": true,       // disk-cache images fetched by UI/proxy
  "video_mode": "link",              // videos are always links, never downloaded
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "fab_vault_dirs": ["F:/EpicShit/VaultCache"],  // auto-detected if omitted
  "headless_refresh": true           // library refresh without visible browser
}
```

## Architecture

```
┌─────────────────────────┐     ┌──────────────────────┐
│   Desktop app (Qt)      │     │  AI agent (MCP stdio) │
│   browser UI (optional) │     │  Claude/Cursor/pi/…   │
└───────────┬─────────────┘     └──────────┬───────────┘
            │                              │
            ▼                              ▼
      ┌─────────────────────────────────────────┐
      │  db.py · semantic.py · local_scan.py    │
      │  ingest.py · store_client.py · unpacker │
      ├─────────────────────────────────────────┤
      │  data/assets.db  (SQLite + FTS5 + vecs) │
      └─────────────────────────────────────────┘
```

All frontends share one backend; WAL mode lets the GUI write while agents read
with zero lock contention.

## Privacy & safety

- Store sessions live only in local browser profiles (`profiles/`) on your machine
- Your library data, caches, and embeddings never leave your disk
- The unpacker refuses paths escaping the target project
- Config registration writes backups before touching anything

## Known limitations

- Store library refresh relies on intercepting the stores' own page traffic
  (resilient to endpoint churn, but a store redesign can temporarily break it —
  CSV import always works)
- Semantic index needs a rebuild after large imports (`python -m src.semantic build`)
- Windows-first (hotkey, disk-cache paths); the rest is cross-platform Python

## License

[MIT](LICENSE) © Tansel Altınel
