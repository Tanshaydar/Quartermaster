# Quartermaster

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

---

## What you get

**Search that understands intent.** SQLite FTS5 keyword ranking fused with local ONNX embeddings via Reciprocal Rank Fusion. An exact product name hits exactly; a phrase like *"spooky abandoned industrial site"* surfaces the right warehouse pack even when it shares no keyword with the title. No cloud API, no vectors leaving the machine.

**Ground truth about your disk.** Scans `%APPDATA%/Unity/Asset Store-5.x/` and Epic's VaultCache, so every result is tagged *already downloaded* or *cloud only*. Agents prefer what's local — a zero-download import beats a 4 GB one.

**A linter for stacks, not just files.** Two weather systems will fight. Two vegetation renderers will fight. A modular shader's add-on installed without its core package silently does nothing. A URP shader in an HDRP project renders pink. Quartermaster knows these before you spend an afternoon on them.

**Direct unpacking.** Extracts a cached `.unitypackage` straight into `Assets/`, skipping Package Manager entirely, and drops `/Demo/`, `/SampleScenes/`, `/Documentation/` and PDFs on the way in — typically 60–80% less disk and compile time per package.

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

---

## Other ways in

- **Desktop app** (`run_desktop.bat`) — PySide6, system tray, `Win+Alt+V` spotlight hotkey.
- **Web UI** (`run_ui.bat`) — `http://localhost:7890`, press `/` to search.
- **Unity Editor** — import `editor_bridge/Quartermaster-Bridge.unitypackage`, then `Window > Quartermaster`. Search and import without leaving the editor.

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
