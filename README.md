# ⚡ VaultMCP

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP Ready](https://img.shields.io/badge/MCP-Protocol%20Ready-success.svg)](https://modelcontextprotocol.io)
[![Platform: Windows 11 / Native](https://img.shields.io/badge/Platform-Windows%20%7C%20Cross--Platform-lightgrey.svg)]()
[![FastEmbed CPU](https://img.shields.io/badge/FastEmbed-Offline%20Vectors%20(CPU)-purple.svg)](https://github.com/qdrant/fastembed)

**Your game-asset vault, queryable and executable by AI agents.**

*Ground your AI pair programmer in the 1,400+ Unity and Unreal/Fab assets you already own — with zero hallucinations, hybrid semantic search, stack conflict linting, and 1-click project unpacking.*

[Features](#-key-features) • [Why VaultMCP](#-the-problem-vs-the-solution) • [Quick Start](#-quick-start) • [MCP Tools API](#-mcp-agent-tools-reference) • [In-Engine Unity Bridge](#-in-engine-unity-bridge) • [Architecture](#-architecture)

</div>

---

## 💡 The Problem vs. The Solution

If you have been building games or collecting assets for years, you likely own hundreds of packages you cannot remember. When you ask an AI coding assistant to build a feature, it either **hallucinates assets you don't own** or suggests writing custom shaders and systems from scratch when you already own battle-tested production assets.

| Without VaultMCP ❌ | With VaultMCP ⚡ |
| :--- | :--- |
| **Agent Hallucinations:** AI recommends buying packages you don't own or writing custom boilerplate. | **Grounded Library:** AI inspects your vault and selects the exact packages you own. |
| **Manual Store Scrolling:** Searching Unity Asset Store & Fab purchase tabs to find what you bought. | **Hybrid Semantic Search:** Ask *"spooky abandoned factory with rubble"* to instantly find relevant packs. |
| **Pipeline Mismatches:** Importing a URP-only shader into an HDRP project causing pink shader errors. | **Automated Linter:** VaultMCP sniffs the project and warns before incompatible imports. |
| **Asset Store Download Friction:** Waiting for Package Manager or manually searching `.unitypackage` files. | **1-Click Direct Unpack:** AI or user unpacks local cache directly into `Assets/`, stripping demo bloat (~70% savings). |

---

## 🚀 Key Features

### 1. 🧠 Offline Hybrid Semantic Search (CPU FastEmbed + SQLite FTS5)
* Combines **SQLite FTS5 BM25 keyword ranking** with **CPU-only vector embeddings** (`BAAI/bge-small-en-v1.5` via ONNX).
* Uses **Reciprocal Rank Fusion (RRF)**: Exact keyword queries (`"MicroSplat HDRP"`) hit instantly, while conceptual natural language queries (`"spooky abandoned industrial site"`) retrieve *Warehouse – Abandoned Factory District* with zero external cloud API calls.

### 2. ⚡ Local Disk Cache Scanner (Cloud vs. On-Disk)
* Scans `%APPDATA%\Unity\Asset Store-5.x\` and Epic Games VaultCache directories.
* Matches files by normalized title and tags packages as **`⚡ Downloaded (Local)`** vs. **`☁ Cloud Library`**.
* Agents prioritize local assets for immediate zero-download imports.

### 3. 📦 Direct Project Unpacker with Demo Stripper
* Extracts cached `.unitypackage` archives directly into your active Unity project's `Assets/` folder.
* **Automatic Bloat Stripper:** Filters out heavy `/Demo/`, `/SampleScenes/`, `/Documentation/`, and `.pdf` files, saving **60% to 80% disk space** and compile time per package.
* **Path Traversal Guard:** Sandboxes extractions to prevent directory escapes (`../../`).

### 4. 🛡️ Project Pipeline Auditor & Stack Linter
* **Project Sniffer:** Reads `ProjectSettings/ProjectVersion.txt` and `Packages/manifest.json` for Unity, and `.uproject` / `DefaultEngine.ini` for Unreal to identify the engine version and active render pipeline (`HDRP`, `URP`, `Built-in`).
* **Stack Intelligence:** Uses rules in `data/recipes.json` to detect role conflicts (e.g. two competing weather systems or vegetation renderers) and missing prerequisite packages.
* **Cross-Engine Warning:** Warns when unpacking Unreal/Fab ORM assets into Unity regarding roughness smoothness inversion.

### 5. 🖥️ Native Desktop App & Modern Web UI
* **Native Desktop (PySide6 / Qt):** Windows 11 taskbar integration (`AppUserModelID`), system tray minimization, custom icon, and **`Win + Alt + V` global Spotlight hotkey**.
* **Modern Web Interface:** Dark glassmorphism dashboard running locally on `http://localhost:7890`.

### 6. 🎮 In-Engine Unity Editor Bridge
* Drop `editor_bridge/VaultMCP-Bridge.unitypackage` into any Unity project to search, filter, and 1-click import assets from inside the Unity Editor (`Window > VaultMCP`).

---

## 📦 Quick Start

### Prerequisites
* **Python 3.10+** (Tested on Python 3.11, 3.12, 3.14 on Windows 11).

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/Tanshaydar/VaultMCP.git
cd VaultMCP

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed your library
# Drop your Unity/Fab CSV dumps into data/seed/, then run:
python -m src.ingest

# 4. (Optional) Build semantic embeddings
python -m src.semantic build
```

---

## 🤖 Connect AI Agents in 1 Second

VaultMCP includes an automated registration CLI that safely merges the MCP configuration into your AI tools without overwriting your existing MCP servers:

```bash
# Register with ALL installed agents (Claude Desktop, Cursor, Windsurf, Antigravity)
python -m src.register --all

# Or preview changes without writing
python -m src.register --all --dry-run
```

### Manual Configuration Snippet

If configuring manually, add this to your client's MCP configuration:

```json
{
  "mcpServers": {
    "vaultmcp": {
      "command": "python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "<path-to-VaultMCP>"
    }
  }
}
```

---

## 🛠️ MCP Agent Tools Reference

When connected over `stdio`, AI agents gain access to 9 tools:

| MCP Tool | Description | Key Parameters |
| :--- | :--- | :--- |
| `search_owned_assets` | Hybrid keyword + semantic vector search across your library. | `query`, `engine`, `pipeline`, `category`, `local_only`, `limit` |
| `get_asset_details` | Retrieves full technical metadata, usage notes, gallery links, and store URLs. | `asset_id` |
| `list_asset_categories` | Lists all library categories and asset distribution counts. | *None* |
| `get_stack_recommendations` | Given a game feature prompt (e.g. *"dark fantasy dungeon with volumetric fog"*), maps owned packs to solve each aspect. | `problem_description`, `limit_per_category` |
| `audit_project` | Detects the engine, version, and active render pipeline of a game project. | `project_dir` |
| `import_asset_to_project` | Extracts a locally-downloaded `.unitypackage` into a Unity project with demo stripping & safety checks. | `asset_id`, `project_dir`, `strip_demos` |
| `validate_stack` | Lints a planned stack of asset IDs for role conflicts (dual skyboxes, dual renderers) and missing dependencies. | `asset_ids` |
| `list_stack_recipes` | Returns curated production stacks from `data/recipes.json` resolved against assets **you own**. | *None* |
| `get_vault_stats` | Returns total vault statistics, breakdown by store, and local vs. cloud counts. | *None* |

### Example Agent Prompt & Flow

> **User to Agent:** *"I want to create an atmospheric karst valley level in Unity HDRP with volumetric water and pine vegetation."*
>
> 1. Agent calls `audit_project("D:/Projects/MyLevel")` $\rightarrow$ Detects Unity 6 / HDRP.
> 2. Agent calls `get_stack_recommendations(...)` $\rightarrow$ Recommends `MicroVerse`, `StampIT! Valleys`, `MicroSplat HDRP`, `KWS2 Dynamic Water`, and `Mountain Environment`.
> 3. Agent calls `validate_stack(...)` $\rightarrow$ Checks for conflicting renderers.
> 4. Agent calls `import_asset_to_project(asset_id, project_dir)` $\rightarrow$ Unpacks directly into `Assets/`, stripping unneeded sample scenes.

---

## 🎮 In-Engine Unity Bridge

To use VaultMCP directly inside the Unity Editor without Alt-Tabbing:

1. Open your Unity project.
2. Import `editor_bridge/VaultMCP-Bridge.unitypackage` (or generate a fresh bridge via `python -m src.build_bridge`).
3. In Unity, open **`Window > VaultMCP`**.
4. Search your library, inspect packages, and click **"Import into this project"** or **"Add to Scene"**.

---

## 🖥️ Desktop & Web UI Usage

```bash
# Launch Native Desktop App (PySide6 with Win+Alt+V global hotkey)
run_desktop.bat

# Launch Local Web UI (http://localhost:7890)
run_ui.bat
```

* **Spotlight Hotkey:** Press `Win + Alt + V` anywhere on Windows to toggle the floating Vault window.
* **Quick Search:** Press `/` in the Web UI to focus the search bar.
* **View Modes:** Toggle between rich 16:9 media cards and compact table views.

---

## 🏗️ Architecture

```
┌───────────────────────────────────────────────────────────┐
│                     PRESENTATION LAYER                    │
│  ┌────────────────────────┐    ┌───────────────────────┐  │
│  │ Native Desktop App     │    │ Modern Web Dashboard  │  │
│  │ (PySide6 / WinHot)     │    │ (FastAPI / Blur CSS)  │  │
│  └───────────┬────────────┘    └───────────┬───────────┘  │
│              │                             │              │
│              │      ┌──────────────────────┴───────────┐  │
│              │      │ AI Agents (MCP stdio Server)     │  │
│              │      │ Claude / Cursor / Antigravity    │  │
│              │      └──────────────┬───────────────────┘  │
└──────────────┼─────────────────────┼──────────────────────┘
               ▼                     ▼
┌───────────────────────────────────────────────────────────┐
│                      VAULTMCP BACKEND                     │
│  ┌───────────────────┐  ┌──────────────────────────────┐  │
│  │ Direct Unpacker   │  │ Offline Semantic Search      │  │
│  │ (Safe Extract)    │  │ (FastEmbed BGE-Small ONNX)   │  │
│  └───────────────────┘  └──────────────────────────────┘  │
│  ┌───────────────────┐  ┌──────────────────────────────┐  │
│  │ Local Disk Scan   │  │ Stack Conflict Engine        │  │
│  │ (%APPDATA%/Vault) │  │ (data/recipes.json)          │  │
│  └───────────────────┘  └──────────────────────────────┘  │
│                              │                            │
│                              ▼                            │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ SQLite Database (data/assets.db)                   │  │
│  │ - WAL Mode + 5000ms Busy Timeout (Zero Lock Cont.) │  │
│  │ - FTS5 Virtual Table (BM25 Ranking)                 │  │
│  │ - asset_vectors Table (384-dim Float Embeddings)    │  │
│  └─────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

---

## 🔒 Privacy & Local-First Philosophy

* **100% Local Storage:** Your database, embeddings, disk paths, and session tokens live entirely on your machine in `data/assets.db`.
* **Zero Telemetry:** No external tracking, no cloud telemetry, and no remote servers.
* **Sandboxed Extractions:** The unpacker strictly verifies all tar paths against target roots before extracting files.

---

## 📄 License

Distributed under the [MIT License](LICENSE).

Copyright (c) 2026 **Tanshaydar**.
