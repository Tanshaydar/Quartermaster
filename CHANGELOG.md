# Changelog

All notable changes to Quartermaster are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.5.0] - 2026-11-02

### Added
* **Proactive Agent Instructions**: The MCP server now sends `instructions` when a client connects, telling agents to search the vault before building an asset from scratch or recommending a purchase, and to run `audit_project` and `validate_stack` before importing ([`src/mcp_server.py`](src/mcp_server.py)).
* **Quartermaster Agent Skill**: Added [`skills/quartermaster/SKILL.md`](skills/quartermaster/SKILL.md), the same workflow in the Agent Skills format, with setup steps for when the server isn't connected yet. Standalone release archives now include the `skills/` folder ([`build_standalone.py`](build_standalone.py)).

### Changed
* **MCP SDK Minimum**: `requirements.txt` now asks for `mcp>=1.7`, the first version whose `FastMCP.tool()` accepts annotations.
* **PySide6 6.12.0 Excluded**: Python 3.10 test runs crashed at interpreter shutdown with PySide6 6.12.0 installed, so `requirements.txt` skips that version.

### Fixed
* **Fab & Unity Library Harvests**: Fab's native harvest and Unity's chunk harvest have failed with `name 'ROOT_DIR' is not defined` since `v0.9.1`. The error was caught and only written to the harvest log. The missing import is back ([`src/store_client.py`](src/store_client.py), [#2](https://github.com/Tanshaydar/Quartermaster/pull/2), thanks to [@EnterTG](https://github.com/EnterTG)).
* **Null Quixel Categories**: The Quixel catalog sync now handles entries whose `category` is null ([#2](https://github.com/Tanshaydar/Quartermaster/pull/2)).
* **Tool Annotations**: All nine MCP tools now declare what they do. Eight are marked read-only, and `import_asset_to_project` is marked as writing and destructive, because it overwrites files already at the paths it unpacks to ([`src/mcp_server.py`](src/mcp_server.py)).
* **Malformed `recipes.json`**: A parse error in `data/recipes.json` raised `NameError` instead of warning and falling back to no rules, because `stack_rules` never imported `sys` ([`src/stack_rules.py`](src/stack_rules.py)).

---

## [1.4.0] - 2026-09-29

### Added
* **Embedded UPM Package Routing**: Packages shipping embedded Unity Package Manager trees (`Packages/<name>/package.json`) are now extracted directly to `<project>/Packages/<name>/` rather than being relocated under `Assets/_Quartermaster_Imported/` ([`src/unpacker.py`](src/unpacker.py)).
* **Disk Size Measurement & Tracking**: Disk scanners record downloaded asset sizes on disk (`.unitypackage` file sizes, Fab vault folder sizes) into `size_mb` and `size_str`, preserved across subsequent store syncs ([`src/ingest.py`](src/ingest.py), [`src/db.py`](src/db.py)).
* **Containerization & Glama Deployment**: Added `Dockerfile`, `.dockerignore`, and maintainer metadata (`glama.json`) for containerized introspection and hosting on Glama.
* **MCP Registry Publishing**: Added a GitHub Actions workflow that publishes the server manifest to the official MCP registry using GitHub OIDC, run by hand once the release digest is stamped into `server.json` ([`.github/workflows/publish-mcp.yml`](.github/workflows/publish-mcp.yml)).
* **Deterministic Bridge Packaging**: [`src/build_bridge.py`](src/build_bridge.py) produces byte-identical `.unitypackage` builds with pinned/deterministic GUIDs and mtime-zero gzip headers.

### Fixed
* **Pre-Filtered Hybrid Search**: Filters (category, pipeline, engine, source) are now pushed directly into SQL and vector candidate filtering before taking the top-50 slice. Previously, filtering in Python after slicing caused matching assets past the cap to disappear (e.g. `"wall"` + Fab returned 0 of 51) ([`src/db.py`](src/db.py), [`src/semantic.py`](src/semantic.py)).
* **Search Sort Keys & Tie-breaking**: Corrected dictionary sort keys in hybrid search (`claimed_at` $\rightarrow$ `claimed_date`, `size_bytes` $\rightarrow$ `size_mb`) and added title tie-breaking to match SQL browse ordering. Previously, sorting by "Recently Acquired" or "Size" silently retained relevance order.
* **Desktop Local-Only Truncation**: Pushed `local_only` filtering into SQL instead of slicing 5,000 rows in Python, preventing downloaded assets from vanishing from desktop browse views.
* **Web UI Server-Side Sorting**: Web UI now passes `sort_by` to `/api/assets` so sorting applies across the full vault rather than only client-sorting the first 2,000 fetched rows ([`web/app.js`](web/app.js)).
* **MCP Empty-Query Crash**: Fixed `UnboundLocalError` when calling [`search_owned_assets`](src/mcp_server.py) with empty/whitespace queries, and pushed filters to SQL so empty searches are not artificially capped at 100 items.
* **Store Sync Data Preservation**: [`upsert_asset()`](src/db.py) now preserves non-zero `size_mb`, `size_str`, and `claimed_date` values so subsequent store syncs don't wipe out sizes and acquisition dates.
* **Unity Bridge Compilation**: Fixed compile errors on Unity 2022.3 and Unity 6 in [`QuartermasterWindow.cs`](editor_bridge/QuartermasterWindow.cs) (`System.IO.Directory` qualification, popup index mismatch).
* **Unity Bridge Import Acknowledgement**: Added `status: "ok"` to `/api/import` response so the Unity bridge recognizes successful imports and prompts to add prefabs to scene.
* **Bridge Upgrade Conflicts (CS0101)**: Pinned `v1.0.0` GUIDs in [`src/build_bridge.py`](src/build_bridge.py) so re-importing updated bridge packages updates the existing script in-place instead of creating duplicate classes.
* **Test Suite DB Isolation**: Moved `init_db()` calls out of module import scope in [`src/server.py`](src/server.py) and [`src/mcp_server.py`](src/mcp_server.py) into startup lifespans, preventing unit tests from mutating or pruning local dev vaults (`data/assets.db`).

---

## [1.3.0] - 2026-09-05

### Added
* **Cross-Platform Standalone Distributions**: Pre-built standalone binaries for Windows (`Quartermaster-windows-x64.zip`) and Linux (`Quartermaster-linux-x64.tar.gz`) bundling both the PySide6 desktop UI and headless FastMCP server.
* **Universal Engine Filtering**: Target engine filters (`unity`, `unreal`, `godot`, `all`) now accurately account for cross-engine formats: Quixel Megascans (universal FBX/PBR textures), Leartes Cosmos multi-platform packs, and Fab catalog listings without silent exclusions.
* **Accurate Context Provenance**: Desktop `Copy Context` accurately reports asset source and multi-engine compatibility across Unity, Fab, Quixel, Gumroad, and Cosmos.
* **Marketplace Formats Ingestion**: Enriched Fab and Gumroad ingestion captures declared format specifications and engine compatibility.
* **Official MCP Registry Manifest**: `server.json` packaging manifest stamped with SHA-256 integrity hash for discovery and installation across MCP clients.
* **Optimized 3-Way Hybrid Search**: Sub-85ms RRF fusion across FTS5 keyword, BGE (384-dim) semantic embeddings, and CLIP (512-dim) visual vectors.

---

## [1.2.1] - 2026-09-04

### Changed
* **Brand Identity & UI Refresh**: Monolith brand icon pack deployed across Windows executable (`.ico`), desktop window (`.png`), and web favicon.
* **Warm Terracotta & Brushed Brass Palette**: Replaced electric-blue UI accents across both PySide6 desktop and Web UI with a focused terracotta (`#e06c3a`) and brushed brass (`#d4a34b`) palette.
* **Top Bar Branding**: Added 26×26 brand mark alongside the top-bar title in the desktop app.

### Fixed
* **Qt Mnemonic Underline Fix**: Escaped ampersands (`&&`) in sync dialog group box titles, eliminating unwanted shortcut underline artifacts.

---

## [1.2.0] - 2026-09-04

### Added
* **Zero-Widget Delegate Architecture**: Replaced `setItemWidget` with a lightweight `AssetDelegate(QStyledItemDelegate)`. Scroll frame latency dropped from 70–200ms to ~13ms average.
* **Asynchronous Image Decoding**: Image decoding and downscaling execute strictly inside worker thread pools, eliminating main-thread micro-stutters.
* **List & Grid Views**: Added toggle between compact List view and card-based Grid Gallery view with persistent mode preference.
* **Physical Scan Specs Extraction**: Automatically extracts texel density, scan area, displacement scale, and PBR texture map lists from asset summaries and usage notes.
* **Adaptive FlowLayout**: Wraps map tokens and spec badges without overflowing or horizontal clipping.
* **Quick-Look Modal**: Spacebar preview for assets.

### Fixed
* **Python 3.14 GC Safety**: Hardened long-lived thumbnail references and single-instance socket activation against access violation crashes.
* **Quixel Megascans Aspect Ratio**: Resolved pillarboxing and blowout on ultra-wide Quixel asset banners.
