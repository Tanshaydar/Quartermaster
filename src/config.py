"""
VaultMCP configuration.

Loaded from config.json in the project root; sensible defaults otherwise.
"""
import json
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

DEFAULTS = {
    "server_port": 7890,
    "media_cache_enabled": True,      # proxy & disk-cache remote images
    "media_cache_dir": "cache/images",
    "video_mode": "link",             # "link" = store YouTube/trailer links only, never download
    "enrich_batch_size": 20,
    "enrich_batch_pause": 3,          # seconds between batches (be polite)
    "profiles_dir": "profiles",       # browser profiles for saved logins
    "headless_refresh": False         # headless fetches trigger Unity MFA/bot flags — keep headed
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg.update({k: v for k, v in user_cfg.items()})
        except Exception as e:
            print(f"[warn] Could not parse {CONFIG_PATH}: {e}. Using defaults.")
    # resolve relative dirs against project root
    for key in ("media_cache_dir", "profiles_dir"):
        if not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(ROOT_DIR, cfg[key])
    return cfg


def save_config_partial(updates: dict):
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg.update(updates)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

AUTH_TOKEN_PATH = os.path.join(ROOT_DIR, "data", ".auth_token")
USER_TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".quartermaster", "auth_token")
LOCALAPP_TOKEN_PATH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Quartermaster", "token")


def get_or_create_auth_token() -> str:
    """Returns or generates the per-installation API token.
    Stored in data/.auth_token and mirrored to ~/.quartermaster/auth_token and
    %LOCALAPPDATA%/VaultMCP/token for portable client discovery."""
    cfg = load_config()
    if cfg.get("auth_token"):
        return str(cfg["auth_token"]).strip()

    # Check existing token paths
    for p in (AUTH_TOKEN_PATH, USER_TOKEN_PATH, LOCALAPP_TOKEN_PATH):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    tok = f.read().strip()
                    if tok:
                        return tok
            except Exception:
                pass

    # Generate new cryptographically secure 32-byte hex token
    import secrets
    tok = secrets.token_hex(32)
    for p in (AUTH_TOKEN_PATH, USER_TOKEN_PATH, LOCALAPP_TOKEN_PATH):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(tok)
        except Exception:
            pass
    return tok
