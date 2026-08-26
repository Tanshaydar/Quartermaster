"""
VaultMCP / Quartermaster configuration.

Loaded from config.json; sensible defaults otherwise.
Frozen-aware: separates read-only bundle resources from writable app data.
"""
import json
import os
import sys
from typing import Optional

IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    # PyInstaller onedir bundles static data in sys._MEIPASS (_internal)
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Quartermaster")
else:
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = BUNDLE_DIR

ROOT_DIR = DATA_DIR

# Ensure writable data directory and subfolders exist
os.makedirs(os.path.join(DATA_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "cache", "images"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "profiles"), exist_ok=True)

# Read-only bundled asset paths (resolve against BUNDLE_DIR)
RECIPES_PATH = os.path.join(BUNDLE_DIR, "data", "recipes.json")
CONCEPTS_PATH = os.path.join(BUNDLE_DIR, "data", "concepts.json")
ICON_ICO_PATH = os.path.join(BUNDLE_DIR, "assets", "icon.ico")
ICON_PNG_PATH = os.path.join(BUNDLE_DIR, "assets", "icon.png")
WEB_DIR = os.path.join(BUNDLE_DIR, "web")
SEED_DIR = os.path.join(BUNDLE_DIR, "data", "seed")

# Writable paths (resolve against DATA_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "data", "assets.db")
AUTH_TOKEN_PATH = os.path.join(DATA_DIR, "data", ".auth_token")
CRASH_LOG_PATH = os.path.join(DATA_DIR, "data", "crash.log")
HARVEST_LOG_PATH = os.path.join(DATA_DIR, "data", "store_harvest.log")

__version__ = "1.0.0"


# --------------------------- SSRF & Image Security -------------------------

ALLOWED_IMAGE_DOMAINS = (
    ".unity3d.com", "unity3d.com",
    ".unity.com", "unity.com",
    ".fab.com", "fab.com",
    ".epicgames.com", "epicgames.com",
    ".unrealengine.com", "unrealengine.com",
    ".artstation.com", "artstation.com",
    ".sketchfab.com", "sketchfab.com",
    ".ytimg.com", "ytimg.com",
    ".youtube.com", "youtube.com",
)

MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15MB cap


def is_safe_image_url(target_url: str) -> bool:
    """
    Validates that a URL is strictly HTTP(S), targets an allowlisted CDN domain,
    and does not point to localhost, RFC1918 private ranges, or cloud metadata endpoints.
    """
    import urllib.parse
    import ipaddress
    try:
        parsed = urllib.parse.urlparse(target_url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host or host == "localhost":
            return False

        # Block IP-based host directly (prevent 127.0.0.1, 169.254.169.254, 10.x, 192.168.x)
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass

        return any(host == d or host.endswith(d if d.startswith(".") else "." + d)
                   for d in ALLOWED_IMAGE_DOMAINS)
    except Exception:
        return False


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
    # resolve relative dirs against writable data root
    for key in ("media_cache_dir", "profiles_dir"):
        if not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(DATA_DIR, cfg[key])
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
