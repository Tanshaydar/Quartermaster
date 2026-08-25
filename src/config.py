"""
VaultMCP configuration.

Loaded from config.json in the project root; sensible defaults otherwise.
"""
import json
import os
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

__version__ = "1.0.0"


def evict_image_cache(cache_dir: str, max_files: int = 2000, prune_count: int = 100):
    """Enforce bounded LRU image cache across desktop and server frontends."""
    try:
        if not os.path.isdir(cache_dir):
            return
        cached_files = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir) if not f.startswith(".")]
        if len(cached_files) >= max_files:
            cached_files.sort(key=os.path.getmtime)
            for old_f in cached_files[:prune_count]:
                try:
                    os.remove(old_f)
                except Exception:
                    pass
    except Exception:
        pass


def rotate_log_if_large(log_path: str, max_bytes: int = 5 * 1024 * 1024):
    """Rotate log file if it exceeds maximum size cap (5 MB)."""
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > max_bytes:
            backup = log_path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(log_path, backup)
    except Exception:
        pass

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
LEGACY_USER_TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".vaultmcp", "auth_token")
LEGACY_LOCALAPP_TOKEN_PATH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "VaultMCP", "token")

ALL_TOKEN_PATHS = [
    AUTH_TOKEN_PATH,
    USER_TOKEN_PATH,
    LOCALAPP_TOKEN_PATH,
    LEGACY_USER_TOKEN_PATH,
    LEGACY_LOCALAPP_TOKEN_PATH,
]


_ACTIVE_AUTH_TOKEN: Optional[str] = None


def get_or_create_auth_token() -> str:
    """Returns or generates the per-installation API token.
    Stored in data/.auth_token and actively mirrored to ~/.quartermaster/auth_token,
    %LOCALAPPDATA%/Quartermaster/token, and legacy VaultMCP paths for portable client discovery."""
    global _ACTIVE_AUTH_TOKEN
    if _ACTIVE_AUTH_TOKEN:
        return _ACTIVE_AUTH_TOKEN

    cfg = load_config()
    tok = str(cfg.get("auth_token", "")).strip()

    if not tok:
        # Check existing token paths
        for p in ALL_TOKEN_PATHS:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        found = f.read().strip()
                        if found:
                            tok = found
                            break
                except Exception:
                    pass

    if not tok:
        # Generate new cryptographically secure 32-byte hex token
        import secrets
        tok = secrets.token_hex(32)

    # Sync token to all mirror paths so bridges and CLI find it reliably
    for p in ALL_TOKEN_PATHS:
        try:
            needs_write = True
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as rf:
                    if rf.read().strip() == tok:
                        needs_write = False
            if needs_write:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as wf:
                    wf.write(tok)
        except Exception:
            pass

    _ACTIVE_AUTH_TOKEN = tok
    return tok
