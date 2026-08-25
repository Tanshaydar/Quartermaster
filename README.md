# Quartermaster

A local MCP server and desktop companion that indexes your purchased Unity Asset Store and Fab libraries, so your AI coding agents (Claude, Cursor, Antigravity, etc.) know what you actually own instead of telling you to buy new packs or write boilerplate shaders from scratch.

---

## The Problem

If you've been doing gamedev for a few years, you probably have hundreds of assets across Unity and Fab. When you ask an AI assistant to build a scene or implement a feature, it usually does one of two things:
1. Gives you a generic script that duplicates a plugin you already paid $80 for.
2. Suggests a list of commercial assets to buy, with no idea if they're in your library.

Neither Unity nor Fab provides an open ownership API. Unity killed `/account/purchases`, and Fab has no public export endpoint. 

Quartermaster logs into your accounts via your own browser session (zero automation during auth to avoid bot flags), pulls down your ownership catalog via authenticated GraphQL session replay, and stores everything in a local SQLite database with semantic vector embeddings.

When your agent needs an asset, it queries Quartermaster over MCP.

---

## Features

- **Local Semantic & Keyword Search (Hybrid FTS5 + ONNX):** Searches by exact title or natural language (e.g. *"modular sci-fi interior with airlocks"*). Vector embeddings are generated locally on CPU via FastEmbed; no data leaves your machine.
- **Disk Cache Detection:** Automatically scans your local Unity Asset Store cache (`%APPDATA%/Unity/Asset Store-5.x/`) and Epic `VaultCache` (`FabLibrary/`). Your agent knows which assets are already downloaded and can prioritize zero-download workflows.
- **Stack & Pipeline Linting:** Checks for role conflicts (e.g. two conflicting terrain systems or weather controllers) and catches pipeline mismatches (e.g. importing a URP-only shader into an HDRP project).
- **Direct Unity Package Unpacking:** Extracts `.unitypackage` files directly into your project's `Assets/` directory, automatically stripping heavy demo scenes, samples, videos, and documentation to keep project compile times fast.
- **Multiple Interfaces:**
  - **MCP Server:** Native stdio JSON-RPC for Claude Desktop, Cursor, Antigravity, Windsurf.
  - **Desktop HUD (`run_desktop.bat`):** Quick-access spotlight search (`Win + Alt + V`) and library browser.
  - **Web Dashboard (`run_ui.bat`):** Local web UI at `http://localhost:7890`.
  - **Unity Editor Bridge:** In-editor window (`Window > Quartermaster`) to search and import directly inside Unity.

---

## Quick Start

### 1. Installation

Requires Python 3.10+:

```bash
git clone https://github.com/Tanshaydar/Quartermaster.git
cd Quartermaster
pip install -r requirements.txt
```

### 2. Harvest Your Library

Run the interactive store login. This opens a plain browser window for you to sign in (handles 2FA/MFA normally), then saves your session cookies locally under `profiles/`:

```bash
# Unity Asset Store
python -m src.store_client login unity
python -m src.store_client fetch unity

# Fab / Epic Games
python -m src.store_client login fab
python -m src.store_client fetch fab
```

*(Optional)* Run metadata enrichment to pull high-res preview screenshots, store URLs, and full descriptions:
```bash
python -m src.store_client enrich
```

Build local vector embeddings for semantic search:
```bash
python -m src.semantic build
```

### 3. Register MCP with Your Agent

Auto-configure your installed IDEs/clients (Claude Desktop, Cursor, Windsurf, Antigravity):

```bash
python -m src.register --all
```

To preview changes before modifying config files:
```bash
python -m src.register --all --dry-run
```

---

## MCP Tools Reference

When connected, your agent has access to these tools:

| Tool | Purpose |
| :--- | :--- |
| `search_owned_assets(query, engine, pipeline, category, local_only)` | Hybrid keyword + vector search over your library. |
| `get_asset_details(asset_id)` | Returns full description, usage notes, tags, render pipelines, and store links. |
| `get_stack_recommendations(problem_description)` | Maps a game feature brief to complementary assets in your library. |
| `validate_stack(asset_ids)` | Lints a list of assets for dependency issues and duplicate system roles. |
| `list_stack_recipes()` | Lists curated, production-tested asset combinations matched to your library. |
| `audit_project(project_dir)` | Detects engine version and active render pipeline (HDRP/URP/Built-in) of a project. |
| `import_asset_to_project(asset_id, project_dir)` | Safely extracts a local `.unitypackage` into a Unity project with demo content stripped. |

---

## How It Works Under the Hood

1. **Authentication & Ingestion:** `store_client.py` uses Playwright CDP attachment *after* human sign-in to capture auth tokens and replay the store's internal GraphQL queries with proper CSRF headers.
2. **Storage & FTS:** `assets.db` is a local SQLite database running in WAL mode with FTS5 tokenization and automated schema migrations.
3. **Embeddings:** ONNX-based `fastembed` produces 384-dimensional vector embeddings, cached in memory with SQLite `PRAGMA data_version` invalidation tracking.
4. **Sandboxed Unpacking:** `unpacker.py` inspects `.unitypackage` tar headers, strips traversal characters (`../`, absolute paths, control characters), and ensures all files resolve strictly within the project's `Assets/` tree.

---

## Running Tests

Quartermaster includes a built-in test suite covering sandbox traversal security, SQLite preservation, CSRF defenses, and concurrency:

```bash
python run_tests.py -v
```

---

## Limitations & Notes

- **OS:** Windows-first for local cache path detection (`%APPDATA%/Unity/Asset Store-5.x/` and Epic `VaultCache`). MCP search and web UI are platform-agnostic.
- **Single User:** Designed strictly for single-user local development. Everything lives on `localhost:7890` and `127.0.0.1`.
- **Store Changes:** Since ingestion relies on internal store endpoints, major changes to Unity or Fab web interfaces may require updating the fetch parsers.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
