# ⚡ Quartermaster

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP Ready](https://img.shields.io/badge/MCP-Protocol%20Ready-success.svg)](https://modelcontextprotocol.io)
[![Platform: Windows 11 / Native](https://img.shields.io/badge/Platform-Windows%20%7C%20Cross--Platform-lightgrey.svg)]()
[![FastEmbed CPU](https://img.shields.io/badge/FastEmbed-Offline%20Vectors%20(CPU)-purple.svg)](https://github.com/qdrant/fastembed)

</div>

**You own more game assets than you can remember. Your AI assistant knows about none of them.**

Quartermaster indexes everything you've bought on the Unity Asset Store and Fab, and serves it to your coding agent over MCP. So when you ask for a foggy industrial level, you get the packs already sitting on your drive — not a shopping list, and not a suggestion to write a shader from scratch.

```
you    →  "foggy forest clearing at dusk, Unity HDRP"

agent  →  audit_project(<project>)        reads the engine version and pipeline
          search_owned_assets(...)        ranks your library, flags what's already on disk
          validate_stack([...])           catches two terrain systems in one stack
          import_asset_to_project(...)    unpacks into Assets/, demo content stripped
```

Every result is something you already own. Nothing is invented.

---

## Why doesn't this already exist?

Because neither store will tell you what you own.

Unity killed `/account/purchases` (404 as of August 2026). Fab has no ownership API at all. There is no export button, no OAuth scope, no CSV endpoint. The only machine-readable record of your own purchases lives inside the store's private GraphQL, behind SSO, MFA, and bot detection that rejects every headless browser it sees.

Quartermaster gets it by opening your real browser, letting *you* sign in with zero automation touching the login flow, then attaching a debugger afterward to intercept the store's own `myAssets` ownership list — and replaying its own paginated queries, with its own CSRF headers, to fill in the details.

That plumbing is most of this project. It is also why you can't just write this in an afternoon.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  1. Real Browser Sign-In (0% automation — passes SSO, MFA, & Cloudflare) │
│  2. CDP Debug Attachment (replays authenticated GraphQL queries)         │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                    Quartermaster Local Core (Offline)                    │
│   • SQLite FTS5 (BM25)             • FastEmbed ONNX Vectors (CPU)        │
│   • Disk Cache Sniffer             • Cross-Engine Stack Linter Engine    │
└──────────────┬─────────────────────┬─────────────────────┬───────────────┘
               │                     │                     │
               ▼                     ▼                     ▼
 ┌─────────────────────────┐ ┌───────────────┐ ┌─────────────────────────┐
 │ MCP Server (JSON-RPC)   │ │ Desktop & Web │ │ Unity Editor (Bridge)   │
 │ Claude / Cursor / AGY   │ │ (Win+Alt+V)   │ │ Window > Quartermaster  │
 └─────────────────────────┘ └───────────────┘ └─────────────────────────┘
```

---

## What you get

**Search that understands intent.** SQLite FTS5 keyword ranking fused with local ONNX embeddings via Reciprocal Rank Fusion. An exact product name hits exactly; a phrase like *"spooky abandoned industrial site"* surfaces the right warehouse pack even when it shares no keyword with the title. No cloud API, no vectors leaving the machine.

**Ground truth about your disk.** Scans `%APPDATA%/Unity/Asset Store-5.x/` and Epic's VaultCache (`FabLibrary/`), so every result is tagged *already downloaded* or *cloud only*. Agents prefer what's local — a zero-download import beats a 4 GB one.

**A linter for stacks, not just files.** Two weather systems will fight. Two vegetation renderers will fight. A modular shader's add-on installed without its core package silently does nothing. A URP shader in an HDRP project renders pink. Quartermaster knows these before you spend an afternoon on them.

**Direct unpacking.** Extracts a cached `.unitypackage` straight into `Assets/`, skipping Package Manager entirely, and drops `/Demo/`, `/SampleScenes/`, `/Documentation/` and PDFs on the way in — typically 60–80% less disk and compile time per package.

---

## Capabilities by Engine

| Capability | Unity Asset Store | Fab / Unreal Engine |
| :--- | :---: | :---: |
| **Ownership Harvest** | ✅ CDP Session Replay & CSV | ✅ CDP Session Replay & CSV |
| **Hybrid & Semantic Search** | ✅ SQLite FTS5 + ONNX | ✅ SQLite FTS5 + ONNX |
| **Local Cache Detection** | ✅ `%APPDATA%/Unity/Asset Store-5.x` | ✅ Epic `VaultCache` (`FabLibrary/`) |
| **Stack Conflict Linting** | ✅ Cross-Engine Role Matching | ✅ Cross-Engine Role Matching |
| **Pipeline Compatibility Linting** | ✅ HDRP / URP / Built-in | ❌ *N/A (Uniform format)* |
| **Direct Unpacking (`.unitypackage`)** | ✅ Direct to `Assets/` (demos stripped) | ❌ *Manual via Epic Launcher* |
| **In-Editor Native Window** | ✅ `Window > Quartermaster` | ℹ️ *Desktop / Web / MCP only* |

---

## Install

```bash
git clone https://github.com/Tanshaydar/Quartermaster.git
cd Quartermaster
pip install -r requirements.txt
```

Seed your library one of two ways:

```bash
# A. Live harvest — opens your browser, you sign in, it reads your account
python -m src.store_client login unity
python -m src.store_client fetch unity

# B. From CSV — if you already exported from the store
python -m src.ingest
```

Then connect your agent:

```bash
python -m src.register --all          # Claude Desktop, Cursor, Windsurf, Antigravity
python -m src.register --all --dry-run   # preview first
```

`register` merges into existing MCP config and backs it up first; it will not clobber your other servers.

---

## Agent tools

| Tool | What it answers |
| :--- | :--- |
| `search_owned_assets` | "What do I own that fits this?" — hybrid keyword + semantic |
| `get_asset_details` | Full metadata, usage notes, gallery, store URL |
| `get_stack_recommendations` | "Build me a stack for X" — maps a feature brief onto owned packs |
| `validate_stack` | "Will these fight each other?" — role conflicts, missing prerequisites |
| `list_stack_recipes` | Curated production stacks resolved against what you actually own |
| `audit_project` | Engine, version, render pipeline of a target project |
| `import_asset_to_project` | Unpack a local package into `Assets/`, demos stripped |
| `list_asset_categories` / `get_vault_stats` | Library shape and counts |

### What your agent actually sees

When an agent invokes `search_owned_assets("abandoned warehouse", limit=1)`:

```json
{
  "count": 1,
  "search_mode": "hybrid",
  "results": [
    {
      "id": "unity_000000",
      "engine": "unity",
      "title": "Modular Industrial Warehouse",
      "publisher": "Acme Assets",
      "category": "3D Environments & Props",
      "version": "1.2.0",
      "pipelines": ["HDRP", "URP"],
      "tags": ["industrial", "modular", "warehouse", "pbr"],
      "size": "450 MB",
      "local": true,
      "local_path": "C:/Users/.../Asset Store-5.x/Acme/ModularWarehouse.unitypackage",
      "summary": "Modular industrial structure kit with 4k PBR textures and interior props.",
      "usage_notes": "Check render pipeline setup before placing prefabs.",
      "store_url": "https://assetstore.unity.com/packages/...",
      "match": "both",
      "relevance": 0.032
    }
  ]
}
```

---

## Other ways in

- **Desktop app** (`run_desktop.bat`) — PySide6 native spotlight & tray manager.
- **Web UI** (`run_ui.bat`) — `http://localhost:7890`, press `/` to search.
- **Unity Editor** — import `editor_bridge/Quartermaster-Bridge.unitypackage`, then `Window > Quartermaster`. Search and import without leaving the editor.

> [!TIP]
> **Desktop Spotlight (`run_desktop.bat`):** Press `Win + Alt + V` anywhere in Windows to summon the system-wide quick search HUD.

---

## Security

The local API binds loopback, which any page in your browser can also reach. So:

- **Every state-changing endpoint requires a token.** Generated on first run, stored at `data/.auth_token`, mirrored to `~/.quartermaster/auth_token` and `%LOCALAPPDATA%/Quartermaster/token` for the Unity bridge. Send it as `X-Quartermaster-Token` or `Authorization: Bearer`. The web UI gets a `SameSite=Strict` cookie automatically.
- **Foreign `Origin`/`Referer` is rejected with 403** even with a valid token, so a cross-site form POST cannot reach the unpacker.
- **The unpacker cannot escape `Assets/`.** Package paths are normalized before use — `..` collapsed, drive letters stripped — and the resolved target is asserted under `<project>/Assets/`. Strays are relocated to `Assets/_Quartermaster_Imported/`, never written outside.
- **The image proxy is domain-allowlisted.** Store CDNs only; direct-IP hosts, private ranges, and metadata endpoints refused; redirects re-validated per hop; 15 MB cap.

Nothing phones home. Your library, embeddings, disk paths, and store sessions stay on the machine. Embeddings are computed on-device.

---

## Limits

- **Windows-first.** Disk scanning assumes Windows cache locations. The MCP server and search are portable; local-import detection is not.
- **One machine, one user.** No sync, no multi-tenancy, no server mode. That is deliberate.
- **Harvesting is scraping.** Unity and Fab can change their internals whenever they like, and have. Expect to need updates.
- **Unpacking is Unity-only.** Fab/Unreal assets are indexed and searchable, but `.unitypackage` extraction obviously doesn't apply to them.

---

## License

MIT. Copyright (c) 2026 Tanshaydar.
