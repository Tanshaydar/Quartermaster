"""
VaultMCP native desktop app (PySide6 / Qt).

  python -m src.desktop

Replaces the browser UI with a real window:
  - instant FTS5 search (debounced), engine/pipeline/category filters
  - asset cards + rich detail panel (cover image, gallery, video links)
  - store sync panel: interactive login, library fetch, enrichment, disk scan
  - system tray + global hotkey Win+Alt+V to show/hide (Spotlight-style)

Backend modules are shared with the MCP server; nothing else changes.
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QEvent, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
)

try:
    from .db import init_db, search_assets, get_asset_by_id, get_stats, get_categories
    from .config import load_config, save_config_partial, rotate_log_if_large, evict_image_cache, __version__, ICON_ICO_PATH, ICON_PNG_PATH, CRASH_LOG_PATH
    from . import store_client, local_scan, vision, semantic
except ImportError:
    from db import init_db, search_assets, get_asset_by_id, get_stats, get_categories
    from config import load_config, save_config_partial, rotate_log_if_large, evict_image_cache, __version__, ICON_ICO_PATH, ICON_PNG_PATH, CRASH_LOG_PATH
    import store_client, local_scan, vision, semantic

ICON_PATH = ICON_ICO_PATH
ICON_PNG = ICON_PNG_PATH

ACCENT = "#4f8cff"
GREEN = "#3fb950"
BG = "#0d1117"
PANEL = "#161b22"
CARD = "#1c2129"
BORDER = "#2d333b"
TEXT = "#e6edf3"
MUTED = "#8b949e"

STYLE = f"""
QMainWindow, QDialog {{ background: {BG}; color: {TEXT}; }}
QWidget {{ font-family: 'Segoe UI'; font-size: 13px; }}
QLineEdit, QComboBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 7px 10px; color: {TEXT}; selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QPushButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 7px 14px; color: {TEXT}; font-weight: 500;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton.chip, QPushButton[class="chip"] {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 13px;
    padding: 4px 12px; color: {MUTED}; font-size: 12px; font-weight: 500;
}}
QPushButton.chip:hover, QPushButton[class="chip"]:hover {{ color: {TEXT}; border-color: {ACCENT}; background: {CARD}; }}
QPushButton.chip:checked, QPushButton[class="chip"]:checked {{
    background: #1f2a3c; color: {ACCENT}; border: 1px solid {ACCENT}; font-weight: 600;
}}
QPushButton.view_toggle, QPushButton[class="view_toggle"] {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 5px 10px; color: {MUTED}; font-size: 13px; font-weight: 600;
}}
QPushButton.view_toggle:hover, QPushButton[class="view_toggle"]:hover {{ color: {TEXT}; border-color: {ACCENT}; }}
QPushButton.view_toggle:checked, QPushButton[class="view_toggle"]:checked {{
    background: #1f2a3c; color: {ACCENT}; border: 1px solid {ACCENT}; font-weight: 700;
}}
QListWidget {{
    background: {BG}; border: none; outline: none;
}}
QListWidget::item {{ margin: 3px 6px; border-radius: 8px; }}
QListWidget::item:selected {{ background: {CARD}; border: 1px solid {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
a {{ color: {ACCENT}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
"""


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class SearchWorker(QThread):
    """Runs 3-way hybrid search or DB queries off the UI thread."""
    results_ready = Signal(int, list, str)

    def __init__(self, query_id: int, query: str, eng: Optional[str], pipe: Optional[str],
                 cat: Optional[str], sort_mode: str, local_only: bool = False, parent=None):
        super().__init__(parent)
        self.query_id = query_id
        self.query = query
        self.eng = eng
        self.pipe = pipe
        self.cat = cat
        self.sort_mode = sort_mode
        self.local_only = local_only

    def run(self):
        try:
            if self.query:
                merged = semantic.hybrid_search(self.query, limit=5000)
                items = merged.get("results", [])
                mode = merged.get("search_mode", "3-way-hybrid")

                filtered = []
                for item in items:
                    if self.local_only and not item.get("local_path"):
                        continue
                    if self.eng and item.get("source") != self.eng:
                        continue
                    if self.cat and item.get("category") != self.cat:
                        continue
                    if self.pipe:
                        pipes = item.get("render_pipelines") or item.get("formats") or []
                        if not any(self.pipe.lower() in p.lower() for p in pipes):
                            continue
                    filtered.append(item)

                if self.sort_mode == "title_asc":
                    filtered.sort(key=lambda x: (x.get("title") or "").lower())
                elif self.sort_mode == "title_desc":
                    filtered.sort(key=lambda x: (x.get("title") or "").lower(), reverse=True)
                elif self.sort_mode == "claimed_desc":
                    filtered.sort(key=lambda x: x.get("claimed_at") or "", reverse=True)
                elif self.sort_mode == "size_desc":
                    filtered.sort(key=lambda x: x.get("size_bytes") or 0, reverse=True)
                # "relevance" keeps hybrid_search RRF order

                self.results_ready.emit(self.query_id, filtered, mode)
            else:
                db_sort = self.sort_mode if self.sort_mode != "relevance" else "title_asc"
                items = search_assets(query=None, source=self.eng, pipeline=self.pipe,
                                      category=self.cat, sort_by=db_sort, limit=5000)
                if self.local_only:
                    items = [it for it in items if it.get("local_path")]
                self.results_ready.emit(self.query_id, items, "browse")
        except Exception:
            db_sort = self.sort_mode if self.sort_mode != "relevance" else "title_asc"
            items = search_assets(query=self.query or None, source=self.eng, pipeline=self.pipe,
                                  category=self.cat, sort_by=db_sort, limit=5000)
            if self.local_only:
                items = [it for it in items if it.get("local_path")]
            self.results_ready.emit(self.query_id, items, "keyword")


class LongOp(QThread):
    """Runs a blocking backend call off the UI thread with thread-safe progress marshaling."""
    done = Signal(str, bool)
    progress = Signal(object, object, str)

    def __init__(self, fn, label, success_text=None, parent=None):
        super().__init__(parent)
        self.fn, self.label, self.success_text = fn, label, success_text

    def run(self):
        try:
            def safe_progress(done=None, total=None, text=None):
                self.progress.emit(done, total, text)

            import inspect
            try:
                sig = inspect.signature(self.fn)
                params = len(sig.parameters)
            except (ValueError, TypeError):
                params = 0

            if params >= 1:
                result = self.fn(safe_progress)
            else:
                result = self.fn()

            if result is False:
                self.done.emit(f"{self.label}: not completed.", False)
                return
            if self.success_text:
                self.done.emit(self.success_text, True)
                return
            msg = self.label
            if isinstance(result, dict) and "matched_to_library" in result:
                msg += f" — {result['matched_to_library']} on disk"
            elif isinstance(result, int):
                msg += f" — {result}"
            self.done.emit(msg + " ✓", True)
        except Exception as e:
            self.done.emit(f"{self.label} failed: {e}", False)


class UpdateChecker(QThread):
    """Checks for latest release from GitHub API off the UI thread (non-blocking, fails gracefully)."""
    update_available = Signal(str, str)  # (version_tag, release_url)

    def run(self):
        try:
            import httpx
            r = httpx.get(
                "https://api.github.com/repos/Tanshaydar/Quartermaster/releases/latest",
                timeout=4.0,
                follow_redirects=True,
                headers={"User-Agent": "Quartermaster-Desktop"}
            )
            if r.status_code == 200:
                data = r.json()
                tag = str(data.get("tag_name") or "").lstrip("v").strip()
                if not tag:
                    return
                cur_parts = [int(p) for p in __version__.split(".") if p.isdigit()]
                lat_parts = [int(p) for p in tag.split(".") if p.isdigit()]
                if lat_parts > cur_parts:
                    url = data.get("html_url") or "https://github.com/Tanshaydar/Quartermaster/releases/latest"
                    self.update_available.emit(f"v{tag}", url)
        except Exception:
            pass


class ThumbnailManager(QObject):
    """Central, long-lived image pipeline with in-memory caching and bounded thread pool."""
    image_loaded = Signal(str, bytes)  # (url, raw_bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: Dict[str, bytes] = {}
        self._pix_cache: Dict[tuple, QPixmap] = {}
        self._in_flight: set = set()
        self._shutting_down: bool = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(6)
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)

    def shutdown(self):
        self._shutting_down = True
        try:
            self._pool.clear()
            self._pool.waitForDone(400)
        except Exception:
            pass

    def get_pixmap(self, url: str, w: int, h: int) -> Optional[QPixmap]:
        if not url:
            return None
        k = (url, w, h)
        if k in self._pix_cache:
            return self._pix_cache[k]
        if url in self._cache:
            pm = QPixmap()
            if pm.loadFromData(self._cache[url]):
                scaled = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
                self._pix_cache[k] = scaled
                return scaled
        return None

    def request(self, url: str):
        if self._shutting_down or not url or url in self._cache or url in self._in_flight:
            return
        self._in_flight.add(url)
        task = _ImageDownloadTask(url, self)
        self._pool.start(task)

    def _notify(self, url: str, data: bytes):
        if self._shutting_down:
            return
        try:
            self._in_flight.discard(url)
            if data:
                self._cache[url] = data
                self.image_loaded.emit(url, data)
        except (RuntimeError, Exception):
            pass


class _ImageDownloadTask(QRunnable):
    """Worker task run in ThumbnailManager thread pool."""

    def __init__(self, url: str, manager: ThumbnailManager):
        super().__init__()
        self.url = url
        self.manager = manager

    @classmethod
    def cached_path(cls, cfg, url: str) -> str:
        import hashlib
        d = cfg["media_cache_dir"]
        os.makedirs(d, exist_ok=True)
        key = hashlib.sha1(url.encode()).hexdigest()
        ext = ".jpg"
        for e in (".png", ".webp", ".gif", ".jpeg"):
            if e in url.lower():
                ext = e
                break
        return os.path.join(d, key + ext)

    def run(self):
        if getattr(self.manager, "_shutting_down", False):
            return
        data = None
        try:
            cfg = load_config()
            path = self.cached_path(cfg, self.url) if cfg["media_cache_enabled"] else None
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
            else:
                if getattr(self.manager, "_shutting_down", False):
                    return
                import httpx
                r = httpx.get(self.url, timeout=12, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0", "Referer": self.url})
                if r.status_code == 200 and len(r.content) <= 15 * 1024 * 1024:
                    data = r.content
                    if path:
                        evict_image_cache(cfg["media_cache_dir"])
                        with open(path, "wb") as f:
                            f.write(data)
        except Exception:
            pass
        finally:
            try:
                self.manager._notify(self.url, data or b"")
            except (RuntimeError, Exception):
                pass


# Global singleton instance initialized on main thread
THUMB_MGR: Optional[ThumbnailManager] = None


def get_thumb_manager() -> ThumbnailManager:
    global THUMB_MGR
    if THUMB_MGR is None:
        THUMB_MGR = ThumbnailManager()
    return THUMB_MGR



# ---------------------------------------------------------------------------
# Windows global hotkey (Win+Alt+V) via native event filter
# ---------------------------------------------------------------------------

HOTKEY_ID = 0xB00B


class WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.registered = False

    def install(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            import ctypes.wintypes
            MOD_ALT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0008, 0x4000
            ok = ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID,
                                                     MOD_ALT | MOD_WIN | MOD_NOREPEAT, ord('V'))
            self.registered = bool(ok)
        except Exception:
            pass

    def nativeEventFilter(self, event_type, message):
        if sys.platform == "win32" and self.registered:
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:  # WM_HOTKEY
                self.callback()
                return True, 0
        return False, 0


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

def badge(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:8px;"
                      f"padding:1px 7px; font-size:11px; font-weight:600;")
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lbl


def _extract_scan_specs(item: dict) -> dict:
    usage = item.get("usage_notes") or ""
    summary = item.get("summary") or ""
    text = f"{usage} {summary}"
    specs = {}
    m_td = re.search(r"Texel\s*density:?\s*([0-9]+\s*px/m)", text, re.I)
    if m_td:
        specs["texel_density"] = m_td.group(1).strip()
    m_sa = re.search(r"(?:Scan\s*area|Physical\s*size):?\s*([0-9xX\.\s\w]+?)(?:·|\n|\)|$)", text, re.I)
    if m_sa and re.search(r"[0-9]", m_sa.group(1)):
        specs["scan_area"] = m_sa.group(1).strip()
    m_ds = re.search(r"Displacement\s*scale:?\s*([0-9\.]+)", text, re.I)
    if m_ds:
        specs["displacement_scale"] = m_ds.group(1).strip()
    m_maps = re.search(r"Maps:?\s*([^·\n\r]+)", text, re.I)
    if m_maps:
        raw_m = re.sub(r"<[^>]+>", " ", m_maps.group(1))
        raw_m = re.sub(r"\([^)]*\)", " ", raw_m)
        tokens = [w.strip() for w in re.split(r"[\s,]+", raw_m) if w.strip()]
        specs["maps"] = tokens
    return specs


class AssetCard(QWidget):
    """Compact list card: 64x48 rounded thumbnail + title / badges / specs teaser."""

    def __init__(self, item: dict):
        super().__init__()
        self.asset = item
        self.image_url = (item.get("image_url") or "").strip()
        main_lay = QHBoxLayout(self)
        main_lay.setContentsMargins(8, 6, 8, 6)
        main_lay.setSpacing(12)

        # Thumbnail
        self.thumb = QLabel()
        self.thumb.setFixedSize(64, 48)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(f"background:{PANEL}; border:1px solid {BORDER}; border-radius:6px; font-size:16px; color:{MUTED};")
        mgr = get_thumb_manager()
        if self.image_url:
            pm = mgr.get_pixmap(self.image_url, 64, 48)
            if pm:
                self.thumb.setPixmap(pm)
            else:
                self.thumb.setText("🖼")
                mgr.image_loaded.connect(self._on_image_loaded)
                mgr.request(self.image_url)
        else:
            self.thumb.setText("📦" if item.get("source") == "unity" else "🌿")

        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        title = QLabel(item["title"])
        title.setStyleSheet(f"font-weight:600; font-size:13px; color:{TEXT};")
        title.setWordWrap(False)
        row1.addWidget(title, 1)

        if item.get("local_path"):
            loc = badge("⚡ Local", GREEN)
            loc.setToolTip(item["local_path"])
            row1.addWidget(loc)

        if item.get("source") == "unity":
            eng = badge("Unity", "#7c9c47")
        elif item.get("source") == "fab":
            eng = badge("Fab", "#388bfd")
        elif item.get("source") == "quixel":
            eng = badge("Quixel", "#e3b341")
        else:
            eng = badge(str(item.get("source", "")).title(), "#c7c7c7")
        row1.addWidget(eng)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        pub = QLabel(item.get("publisher") or "")
        pub.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        row2.addWidget(pub)

        cat = badge(item.get("category", ""), "#a371f7")
        row2.addWidget(cat)

        if item.get("match"):
            m_badge = badge(item["match"], "#58a6ff")
            row2.addWidget(m_badge)

        spec_text = ""
        summary = item.get("summary") or ""
        m_spec = re.search(r"\(([^)]*(?:px/m|m)[^)]*)\)", summary)
        if m_spec:
            spec_text = m_spec.group(1)
        elif item.get("size_str"):
            spec_text = item["size_str"]

        if spec_text:
            sz = QLabel(spec_text)
            sz.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            row2.addWidget(sz)

        row2.addStretch()

        for w in (title, pub, self.thumb):
            w.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        lay.addLayout(row1)
        lay.addLayout(row2)

        main_lay.addWidget(self.thumb)
        main_lay.addLayout(lay, 1)

    def _on_image_loaded(self, url: str, data: bytes):
        if url == self.image_url:
            pm = get_thumb_manager().get_pixmap(self.image_url, 64, 48)
            if pm:
                self.thumb.setPixmap(pm)


class AssetGridCard(QWidget):
    """Gallery card: 196x216px with 16:9 thumbnail preview, title, publisher, engine, and badges."""

    def __init__(self, item: dict):
        super().__init__()
        self.asset = item
        self.image_url = (item.get("image_url") or "").strip()
        self.setFixedSize(196, 216)
        self.setStyleSheet(f"background:{CARD}; border:1px solid {BORDER}; border-radius:8px;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Thumbnail
        self.thumb = QLabel()
        self.thumb.setFixedSize(196, 120)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(f"background:{PANEL}; border-top-left-radius:7px; border-top-right-radius:7px; font-size:24px; color:{MUTED};")
        mgr = get_thumb_manager()
        if self.image_url:
            pm = mgr.get_pixmap(self.image_url, 196, 120)
            if pm:
                self.thumb.setPixmap(pm)
            else:
                self.thumb.setText("🖼")
                mgr.image_loaded.connect(self._on_image_loaded)
                mgr.request(self.image_url)
        else:
            self.thumb.setText("📦" if item.get("source") == "unity" else "🌿")
        lay.addWidget(self.thumb)

        # Body
        body = QWidget()
        body.setStyleSheet("background: transparent; border: none;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(8, 6, 8, 8)
        blay.setSpacing(3)

        title = QLabel(item["title"])
        title.setStyleSheet(f"font-weight:600; font-size:12px; color:{TEXT}; line-height: 1.2;")
        title.setWordWrap(True)
        title.setFixedHeight(32)
        blay.addWidget(title)

        meta = QHBoxLayout()
        meta.setSpacing(4)
        pub = QLabel(item.get("publisher") or "")
        pub.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        meta.addWidget(pub, 1)

        if item.get("source") == "unity":
            eng = badge("Unity", "#7c9c47")
        elif item.get("source") == "fab":
            eng = badge("Fab", "#388bfd")
        elif item.get("source") == "quixel":
            eng = badge("Quixel", "#e3b341")
        else:
            eng = badge(str(item.get("source", "")).title(), "#c7c7c7")
        meta.addWidget(eng)
        blay.addLayout(meta)

        row3 = QHBoxLayout()
        row3.setSpacing(4)
        if item.get("local_path"):
            loc = badge("⚡ Local", GREEN)
            row3.addWidget(loc)

        summary = item.get("summary") or ""
        m_spec = re.search(r"\(([^)]*(?:px/m|m)[^)]*)\)", summary)
        if m_spec:
            spec_lbl = QLabel(m_spec.group(1))
            spec_lbl.setStyleSheet(f"color:{MUTED}; font-size:10px;")
            row3.addWidget(spec_lbl)
        row3.addStretch()
        blay.addLayout(row3)

        lay.addWidget(body)

        for w in (title, pub, self.thumb):
            w.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def _on_image_loaded(self, url: str, data: bytes):
        if url == self.image_url:
            pm = get_thumb_manager().get_pixmap(self.image_url, 196, 120)
            if pm:
                self.thumb.setPixmap(pm)


class QuickLookDialog(QDialog):
    """Spacebar Quick-Look modal preview."""

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.img_url = (item.get("image_url") or "").strip()
        self.setWindowTitle(item.get("title", "Asset Preview"))
        self.resize(760, 560)
        self.setStyleSheet(f"""
            QDialog {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 10px; }}
            QLabel {{ background: transparent; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        # Image preview
        self.img_label = QLabel("Loading preview…")
        self.img_label.setFixedHeight(360)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet(f"background:{CARD}; border:1px solid {BORDER}; border-radius:8px;")
        lay.addWidget(self.img_label)

        mgr = get_thumb_manager()
        if self.img_url:
            pm = mgr.get_pixmap(self.img_url, 720, 360)
            if pm:
                self.img_label.setPixmap(pm)
            else:
                mgr.image_loaded.connect(self._on_image_loaded)
                mgr.request(self.img_url)
        else:
            self.img_label.setText("No preview image available")

        title = QLabel(f"<h2 style='margin:0; color:{TEXT}'>{item['title']}</h2>")
        title.setWordWrap(True)
        lay.addWidget(title)

        eng = "Unity Asset Store" if item.get("source") == "unity" else ("Fab (Unreal)" if item.get("source") == "fab" else "Quixel Megascans")
        sub = f"{item.get('publisher') or '—'} · {eng} · {item.get('category', '')}"
        if item.get("size_str"):
            sub += f" · {item['size_str']}"
        sub_lbl = QLabel(sub)
        sub_lbl.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        lay.addWidget(sub_lbl)

        # Specs
        specs = _extract_scan_specs(item)
        if specs.get("scan_area") or specs.get("texel_density"):
            spec_parts = []
            if specs.get("scan_area"):
                spec_parts.append(f"Scan area: {specs['scan_area']}")
            if specs.get("texel_density"):
                spec_parts.append(f"Texel density: {specs['texel_density']}")
            if specs.get("displacement_scale"):
                spec_parts.append(f"Displacement scale: {specs['displacement_scale']}")
            sl = QLabel(" · ".join(spec_parts))
            sl.setStyleSheet(f"color:{ACCENT}; font-size:12px; font-weight:600;")
            lay.addWidget(sl)

        # Buttons
        btns = QHBoxLayout()
        btns.setSpacing(8)
        local = item.get("local_path")
        if local and os.path.exists(local):
            rev_btn = QPushButton("⚡ Reveal in Explorer")
            rev_btn.clicked.connect(lambda: self._reveal_local(local))
            btns.addWidget(rev_btn)

        if item.get("store_url"):
            store_btn = QPushButton("↗ Open in Store")
            store_btn.clicked.connect(lambda: webbrowser.open(item["store_url"]))
            btns.addWidget(store_btn)

        btns.addStretch()
        close_btn = QPushButton("Close (Esc)")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        lay.addLayout(btns)

    def _on_image_loaded(self, url: str, data: bytes):
        if url == self.img_url:
            pm = get_thumb_manager().get_pixmap(self.img_url, 720, 360)
            if pm:
                self.img_label.setPixmap(pm)

    def _reveal_local(self, path: str):
        target_dir = os.path.dirname(path) if os.path.isfile(path) else path
        if sys.platform == "win32":
            os.startfile(target_dir)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", target_dir])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", target_dir])

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            self.accept()
            return
        super().keyPressEvent(ev)


class SyncDialog(QDialog):
    """Clean, structured modal for library sync, logins, and AI index maintenance."""

    def __init__(self, main_win, parent=None):
        super().__init__(parent or main_win)
        self.main_win = main_win
        self.setWindowTitle("Library Sync & Maintenance")
        self.resize(680, 500)
        self.setStyleSheet(f"""
            QDialog {{ background: {BG}; color: {TEXT}; }}
            QGroupBox {{
                background: {PANEL};
                border: 1px solid {BORDER};
                border-radius: 8px;
                margin-top: 14px;
                padding-top: 14px;
                font-weight: 600;
                font-size: 13px;
                color: {ACCENT};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        head = QLabel("<h2 style='margin:0'>Library Sync & Maintenance</h2>")
        sub = QLabel("Manage your store sessions, download caches, and AI search indexes.")
        sub.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        lay.addWidget(head)
        lay.addWidget(sub)

        # 1. Unity Section
        u_box = QGroupBox("📦 Unity Asset Store")
        u_lay = QVBoxLayout(u_box)
        self.u_status = QLabel("Checking status…")
        self.u_status.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        u_btns = QHBoxLayout()
        b_u_login = QPushButton("🔐 Sign in Unity")
        b_u_login.clicked.connect(lambda: self._run_op(
            lambda ev: store_client.interactive_login("unity", cancel_event=ev),
            "Unity login",
            pre_status="Sign into Unity Asset Store in browser window, then close window when done."
        ))
        b_u_fetch = QPushButton("⟳ Fetch Library")
        b_u_fetch.clicked.connect(lambda: self.main_win._fetch_op("unity"))
        b_u_scan = QPushButton("⚡ Scan Cache")
        b_u_scan.clicked.connect(lambda: self._run_op(
            lambda ev: local_scan.scan_all(), "Unity cache scan"
        ))
        for b in (b_u_login, b_u_fetch, b_u_scan):
            u_btns.addWidget(b)
        u_btns.addStretch()
        u_lay.addWidget(self.u_status)
        u_lay.addLayout(u_btns)
        lay.addWidget(u_box)

        # 2. Fab & Quixel Section
        f_box = QGroupBox("🌿 Epic Games Fab & Quixel Megascans")
        f_lay = QVBoxLayout(f_box)
        self.f_status = QLabel("Checking status…")
        self.f_status.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        f_btns = QHBoxLayout()
        b_f_login = QPushButton("🔐 Sign in Fab")
        b_f_login.clicked.connect(lambda: self._run_op(
            lambda ev: store_client.interactive_login("fab", cancel_event=ev),
            "Fab login",
            pre_status="Complete Epic login in browser, then close window when done."
        ))
        b_f_fetch = QPushButton("⟳ Fetch Fab")
        b_f_fetch.clicked.connect(lambda: self.main_win._fetch_op("fab"))
        b_q_sync = QPushButton("🌿 Sync Quixel")
        b_q_sync.clicked.connect(lambda: self._run_op(
            lambda ev, cb: store_client.sync_quixel_catalog(cancel_event=ev, progress=cb),
            "Sync Quixel",
            with_progress=True
        ))
        b_q_specs = QPushButton("📐 Quixel Specs")
        b_q_specs.clicked.connect(lambda: self._run_op(
            lambda ev, cb: store_client.enrich_quixel_specs(cancel_event=ev, progress=cb),
            "Quixel scan specs",
            with_progress=True
        ))
        for b in (b_f_login, b_f_fetch, b_q_sync, b_q_specs):
            f_btns.addWidget(b)
        f_btns.addStretch()
        f_lay.addWidget(self.f_status)
        f_lay.addLayout(f_btns)
        lay.addWidget(f_box)

        # 3. AI & Indexing Section
        ai_box = QGroupBox("👁 Local Storage & AI Indexing")
        ai_lay = QVBoxLayout(ai_box)
        self.ai_status = QLabel("Checking status…")
        self.ai_status.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        ai_btns = QHBoxLayout()
        b_ai_scan = QPushButton("⚡ Full Disk Scan")
        b_ai_scan.clicked.connect(lambda: self._run_op(
            lambda ev: local_scan.scan_all(), "Full disk scan"
        ))
        b_ai_enrich = QPushButton("🖼 Enrich Media")
        b_ai_enrich.clicked.connect(lambda: self.main_win._start_enrich_sweep())
        b_ai_vision = QPushButton("👁 Vision Pass (CLIP)")
        b_ai_vision.clicked.connect(lambda: self._run_op(
            lambda ev, cb: vision.build(cancel_event=ev, progress=cb), "Vision pass", with_progress=True
        ))
        for b in (b_ai_scan, b_ai_enrich, b_ai_vision):
            ai_btns.addWidget(b)
        ai_btns.addStretch()
        ai_lay.addWidget(self.ai_status)
        ai_lay.addLayout(ai_btns)
        lay.addWidget(ai_box)

        # Bottom row
        bot = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        bot.addStretch()
        bot.addWidget(close_btn)
        lay.addLayout(bot)

        self.refresh_info()

    def refresh_info(self):
        try:
            stats = get_stats()
            total = stats.get("total", 0)
            by_src = stats.get("by_source", {})
            u_owned = by_src.get("unity", 0)
            f_owned = by_src.get("fab", 0)
            q_catalog = by_src.get("quixel", 0)
            self.u_status.setText(f"{u_owned} owned packages in vault · Session: {'Saved' if store_client.has_saved_session('unity') else 'Not saved'}")
            self.f_status.setText(f"{f_owned} owned Fab · {q_catalog} Quixel catalog entries · Session: {'Saved' if store_client.has_saved_session('fab') else 'Not saved'}")
            self.ai_status.setText(f"{total} indexed assets in SQLite · 3-way hybrid search (FTS5 + BGE + CLIP) ready")
        except Exception:
            pass

    def _run_op(self, fn, label, pre_status=None, with_progress=False):
        self.accept()
        self.main_win._long_op(fn, label, pre_status=pre_status, with_progress=with_progress)


class DetailPanel(QScrollArea):
    """Right-hand asset detail view with hero viewer, action bar, and physical specs cards."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.current: Optional[dict] = None
        self.hero_url: str = ""
        self._gallery_btns: Dict[str, QPushButton] = {}
        get_thumb_manager().image_loaded.connect(self._on_image_loaded)

        inner = QWidget()
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(18, 14, 18, 14)
        self.lay.setSpacing(10)
        self.setWidget(inner)
        self.show_placeholder()

    def clear_layout(self, lay):
        if lay is None:
            return
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                w = it.widget()
                w.setParent(None)
                w.deleteLater()
            elif it.layout():
                self.clear_layout(it.layout())
                it.layout().deleteLater()

    def show_placeholder(self):
        self.clear_layout(self.lay)
        lbl = QLabel("Select an asset to inspect it.\n\nSpace: Quick-Look preview · Win+Alt+V: toggle window")
        lbl.setStyleSheet(f"color:{MUTED}; font-size:14px; line-height: 1.5;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(lbl)
        self.lay.addStretch()
        self.current = None

    def section(self, title: str) -> tuple:
        head = QLabel(title.upper())
        head.setStyleSheet(f"color:{ACCENT}; font-size:11px; letter-spacing:1px; font-weight:700;")
        body = QLabel("")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(f"color:#cdd6e0; font-size:13px; line-height:140%;")
        self.lay.addWidget(head)
        self.lay.addWidget(body)
        return head, body

    def show_asset(self, item: dict):
        self.clear_layout(self.lay)
        self.current = item
        self._gallery_btns.clear()

        # 1. Hero Cover Image
        self.hero_url = (item.get("image_url") or "").strip()
        self.hero_cover = QLabel("  loading cover…  " if self.hero_url else "No preview image available")
        self.hero_cover.setFixedHeight(230)
        self.hero_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hero_cover.setStyleSheet(f"background:{CARD}; border:1px solid {BORDER}; border-radius:8px; color:{MUTED}; font-size:13px;")
        self.lay.addWidget(self.hero_cover)
        if self.hero_url:
            pm = get_thumb_manager().get_pixmap(self.hero_url, 460, 230)
            if pm:
                self.hero_cover.setPixmap(pm)
            else:
                get_thumb_manager().request(self.hero_url)

        # 2. Interactive Gallery Thumbnails (under hero)
        gallery = item.get("gallery_images") or []
        distinct_gallery = []
        for g in gallery:
            if g and g not in distinct_gallery:
                distinct_gallery.append(g)
        if len(distinct_gallery) == 1 and distinct_gallery[0] == self.hero_url:
            distinct_gallery = []

        if distinct_gallery:
            grow = QHBoxLayout()
            grow.setSpacing(6)
            for i, g in enumerate(distinct_gallery[:6]):
                gl = QPushButton()
                gl.setFixedSize(70, 48)
                gl.setCursor(Qt.CursorShape.PointingHandCursor)
                gl.setStyleSheet(f"background:{CARD}; border:1px solid {BORDER}; border-radius:6px;")
                gl.clicked.connect(lambda checked=False, url=g: self._switch_hero(url))
                self._gallery_btns[g] = gl
                pm_g = get_thumb_manager().get_pixmap(g, 68, 46)
                if pm_g:
                    gl.setIcon(QIcon(pm_g))
                    gl.setIconSize(QSize(68, 46))
                else:
                    get_thumb_manager().request(g)
                grow.addWidget(gl)
            grow.addStretch()
            self.lay.addLayout(grow)

        # 3. Action Bar Buttons
        act_bar = QHBoxLayout()
        act_bar.setSpacing(8)
        local = item.get("local_path")
        if local and os.path.exists(local):
            rev_btn = QPushButton("⚡ Reveal in Explorer")
            rev_btn.setStyleSheet(f"background:#1f3b2b; color:{GREEN}; border:1px solid #238636; font-weight:600;")
            rev_btn.clicked.connect(lambda: self._reveal_local(local))
            act_bar.addWidget(rev_btn)

        if (item.get("local_path") or "").lower().endswith(".unitypackage"):
            unpack_btn = QPushButton("📥 Unpack to Unity…")
            unpack_btn.clicked.connect(self._unpack_to_project)
            act_bar.addWidget(unpack_btn)

        if item.get("store_url"):
            store_btn = QPushButton("↗ Open Store")
            store_btn.clicked.connect(lambda: webbrowser.open(item["store_url"]))
            act_bar.addWidget(store_btn)

        copy_btn = QPushButton("📋 Copy Context")
        copy_btn.clicked.connect(self._copy_context)
        act_bar.addWidget(copy_btn)
        act_bar.addStretch()
        self.lay.addLayout(act_bar)

        # 4. Header: Title & Meta Subtitle
        title = QLabel(f"<h2 style='margin:4px 0 0 0; color:{TEXT};'>{item['title']}</h2>")
        title.setWordWrap(True)
        self.lay.addWidget(title)

        eng = "Unity Asset Store" if item["source"] == "unity" else ("Fab (Unreal)" if item["source"] == "fab" else "Quixel Megascans")
        sub = f"{item.get('publisher') or '—'} · {eng}"
        if item.get("version"):
            sub += f" · v{item['version']}"
        if item.get("size_str"):
            sub += f" · {item['size_str']}"
        subl = QLabel(sub)
        subl.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        subl.setWordWrap(True)
        self.lay.addWidget(subl)

        # 5. Physical Scan Specs Cards (if available)
        specs = _extract_scan_specs(item)
        if specs.get("scan_area") or specs.get("texel_density") or specs.get("maps"):
            head_sp = QLabel("PHYSICAL SCAN SPECIFICATIONS")
            head_sp.setStyleSheet(f"color:{ACCENT}; font-size:11px; letter-spacing:1px; font-weight:700; margin-top:6px;")
            self.lay.addWidget(head_sp)

            metric_box = QHBoxLayout()
            metric_box.setSpacing(8)

            def make_metric(lbl_title: str, val: str):
                c = QFrame()
                c.setStyleSheet(f"background:{PANEL}; border:1px solid {BORDER}; border-radius:6px; padding:6px;")
                cl = QVBoxLayout(c)
                cl.setContentsMargins(4, 4, 4, 4)
                cl.setSpacing(2)
                t = QLabel(lbl_title.upper())
                t.setStyleSheet(f"color:{MUTED}; font-size:10px; font-weight:700;")
                v = QLabel(val)
                v.setStyleSheet(f"color:{TEXT}; font-size:13px; font-weight:600;")
                cl.addWidget(t)
                cl.addWidget(v)
                return c

            if specs.get("scan_area"):
                metric_box.addWidget(make_metric("Scan Area", specs["scan_area"]))
            if specs.get("texel_density"):
                metric_box.addWidget(make_metric("Texel Density", specs["texel_density"]))
            if specs.get("displacement_scale"):
                metric_box.addWidget(make_metric("Displacement Scale", specs["displacement_scale"]))
            metric_box.addStretch()
            self.lay.addLayout(metric_box)

            if specs.get("maps"):
                maps_row = QHBoxLayout()
                maps_row.setSpacing(6)
                for m in specs["maps"]:
                    mpill = badge(m, "#58a6ff" if m.lower() in ("displacement", "roughness", "normal") else "#8b949e")
                    maps_row.addWidget(mpill)
                maps_row.addStretch()
                self.lay.addLayout(maps_row)

        # 6. Form Details (Category, Pipelines, Formats, License, Acquired, Local)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(4)

        def kv(k, v):
            kl = QLabel(k); kl.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            vl = QLabel(v); vl.setWordWrap(True)
            vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            vl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            form.addRow(kl, vl)

        kv("Category", item.get("category", ""))
        if item.get("render_pipelines"):
            kv("Pipelines", ", ".join(item["render_pipelines"]))
        if item.get("formats"):
            kv("Formats", ", ".join(item["formats"]))
        if item.get("license"):
            kv("License", item["license"])
        if item.get("claimed_date"):
            kv("Acquired", item["claimed_date"])
        kv("On disk", ("⚡ " + local) if local else "☁ cloud only")
        self.lay.addLayout(form)

        # 7. Summary & Usage Notes
        if item.get("summary"):
            _, b = self.section("About"); b.setText(item["summary"])
        if item.get("usage_notes"):
            _, b = self.section("Usage notes"); b.setText(item["usage_notes"])
        if item.get("tags"):
            _, b = self.section("Tags")
            b.setText("  ".join("#" + t for t in item["tags"]))
        if item.get("vision_tags"):
            _, b = self.section("Visual concepts")
            b.setText("  ".join("👁 " + t for t in item["vision_tags"]))

        videos = [v for v in (item.get("video_links") or [])]
        if videos:
            _, b = self.section("Videos")
            b.setOpenExternalLinks(True)
            b.setText("<br>".join(f"<a href='{v}'>{v}</a>" for v in videos))

        self.lay.addStretch()

    def _switch_hero(self, url: str):
        if not hasattr(self, "hero_cover"):
            return
        self.hero_url = url
        pm = get_thumb_manager().get_pixmap(url, 460, 230)
        if pm:
            self.hero_cover.setPixmap(pm)
        else:
            self.hero_cover.setText("  loading cover…  ")
            get_thumb_manager().request(url)

    def _on_image_loaded(self, url: str, data: bytes):
        if hasattr(self, "hero_cover") and url == self.hero_url:
            pm = get_thumb_manager().get_pixmap(url, 460, 230)
            if pm:
                self.hero_cover.setPixmap(pm)
        if url in self._gallery_btns:
            btn = self._gallery_btns[url]
            pm = get_thumb_manager().get_pixmap(url, 68, 46)
            if pm:
                btn.setIcon(QIcon(pm))
                btn.setIconSize(QSize(68, 46))

    def _reveal_local(self, path: str):
        target_dir = os.path.dirname(path) if os.path.isfile(path) else path
        if sys.platform == "win32":
            os.startfile(target_dir)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", target_dir])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", target_dir])

    def _unpack_to_project(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from .unpacker import import_asset_to_project
        a = self.current
        if not a:
            return
        proj = QFileDialog.getExistingDirectory(
            self, "Choose Unity project root", "",
            QFileDialog.Option.ShowDirsOnly)
        if not proj:
            return
        win = self.window()
        if hasattr(win, "_long_op"):
            win._long_op(
                lambda ev: import_asset_to_project(a["id"], proj),
                f"Unpack {a['title']}",
                pre_status=f"Unpacking '{a['title']}' into project…",
                success_text=f"✅ Unpacked '{a['title']}' into {proj}",
                cancellable=False
            )
        else:
            try:
                r = import_asset_to_project(a["id"], proj)
                QMessageBox.information(
                    self, "Imported",
                    f"Unpacked '{r['title']}' into:\n{r['project']}\n\n"
                    f"Files written: {r['written']}  ·  skipped: {r['skipped']}")
            except Exception as e:
                QMessageBox.critical(self, "Import failed", str(e))

    def _copy_context(self):
        a = self.current
        if not a:
            return
        parts = [
            f"Asset: {a['title']}", f"Publisher: {a.get('publisher', '')}",
            f"Engine: {'Unity Asset Store' if a['source'] == 'unity' else 'Fab (Unreal)'}",
            a.get("version") and f"Version: {a['version']}",
            f"Category: {a.get('category', '')}",
            a.get("render_pipelines") and f"Pipelines: {', '.join(a['render_pipelines'])}",
            a.get("formats") and f"Formats: {', '.join(a['formats'])}",
            a.get("tags") and f"Tags: {', '.join(a['tags'])}",
            a.get("summary") and f"About: {a['summary']}",
            a.get("usage_notes") and f"Usage notes: {a['usage_notes']}",
            a.get("local_path") and f"On disk: {a['local_path']}",
            a.get("store_url") and f"Store URL: {a['store_url']}",
        ]
        QApplication.clipboard().setText("\n".join(p for p in parts if p))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.results: List[dict] = []
        self.view_mode = self.cfg.get("desktop_view_mode", "list")
        self.selected_engine: Optional[str] = None
        self.local_only: bool = False
        self.setWindowTitle("Quartermaster")
        self.resize(1280, 800)
        self.setMinimumSize(900, 560)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        # ---- top bar ----
        top = QWidget()
        top.setStyleSheet(f"background:{BG}; border-bottom:1px solid {BORDER};")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(16, 12, 16, 10)
        tl.setSpacing(8)

        # Row 1: Brand logo, spotlight search, view toggle buttons, sync dialog button, update badge, stats label
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        logo = QLabel("Quarter<span style='color:%s'>master</span>" % ACCENT)
        logo.setTextFormat(Qt.TextFormat.RichText)
        logo.setStyleSheet("font-size:19px; font-weight:700;")

        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Search 6,700+ assets… (Ctrl+K)")
        self.search.installEventFilter(self)

        self.btn_view_list = QPushButton("☰")
        self.btn_view_list.setToolTip("List View (Compact)")
        self.btn_view_list.setCheckable(True)
        self.btn_view_list.setProperty("class", "view_toggle")
        self.btn_view_list.setCursor(Qt.CursorShape.PointingHandCursor)

        self.btn_view_grid = QPushButton("☷")
        self.btn_view_grid.setToolTip("Grid View (Visual Discovery)")
        self.btn_view_grid.setCheckable(True)
        self.btn_view_grid.setProperty("class", "view_toggle")
        self.btn_view_grid.setCursor(Qt.CursorShape.PointingHandCursor)

        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_group.addButton(self.btn_view_list)
        self.view_group.addButton(self.btn_view_grid)

        if self.view_mode == "grid":
            self.btn_view_grid.setChecked(True)
        else:
            self.btn_view_list.setChecked(True)

        self.btn_view_list.clicked.connect(lambda: self.set_view_mode("list"))
        self.btn_view_grid.clicked.connect(lambda: self.set_view_mode("grid"))

        self.sync_btn = QPushButton("⚙ Sync")
        self.sync_btn.setToolTip("Library Sync, Logins & AI Indexing")
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_btn.clicked.connect(self._open_sync_dialog)

        self.update_btn = QPushButton("")
        self.update_btn.setVisible(False)
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.setStyleSheet(f"""
            QPushButton {{
                background: #1f3b2b; color: {GREEN}; border: 1px solid #238636;
                border-radius: 12px; padding: 2px 10px; font-size: 11px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #238636; color: #ffffff; }}
        """)

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")

        row1.addWidget(logo)
        row1.addWidget(self.search, 1)
        row1.addWidget(self.btn_view_list)
        row1.addWidget(self.btn_view_grid)
        row1.addWidget(self.sync_btn)
        row1.addWidget(self.update_btn)
        row1.addWidget(self.stats_label)
        tl.addLayout(row1)

        # Row 2: Filter chips & dropdowns
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)

        self.chip_all = QPushButton("All Sources")
        self.chip_unity = QPushButton("Unity")
        self.chip_fab = QPushButton("Fab")
        self.chip_quixel = QPushButton("Quixel")

        chips = [
            (self.chip_all, None),
            (self.chip_unity, "unity"),
            (self.chip_fab, "fab"),
            (self.chip_quixel, "quixel"),
        ]

        for chip, eng_val in chips:
            chip.setCheckable(True)
            chip.setProperty("class", "chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self.chip_group.addButton(chip)
            row2.addWidget(chip)
            chip.clicked.connect(lambda _checked=False, val=eng_val: self._on_engine_chip(val))

        self.chip_all.setChecked(True)

        self.chip_local = QPushButton("⚡ Local Only")
        self.chip_local.setCheckable(True)
        self.chip_local.setProperty("class", "chip")
        self.chip_local.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chip_local.toggled.connect(self._on_local_chip_toggled)
        row2.addWidget(self.chip_local)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setStyleSheet(f"color: {BORDER}; margin: 2px 4px;")
        row2.addWidget(sep)

        self.pipeline = QComboBox()
        self.pipeline.addItems(["All Pipelines", "HDRP", "URP", "Built-in"])
        self.category = QComboBox()
        self.category.addItem("All Categories")
        self.sort_by = QComboBox()
        self.sort_by.addItems(["Relevance", "Name (A-Z)", "Name (Z-A)", "Recently Acquired", "Size (Largest)"])

        row2.addWidget(self.pipeline)
        row2.addWidget(self.category)
        row2.addWidget(self.sort_by)
        row2.addStretch()

        tl.addLayout(row2)

        # Compatibility stubs for sync operations
        self.sync_panel = QWidget()
        self.sync_status = QLabel("")

        root.addWidget(top)

        # ---- splitter: results | detail ----
        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)

        self.list = QListWidget()
        self.list.itemClicked.connect(self._on_select)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._on_select(cur) if cur else None)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.installEventFilter(self)
        self.list.setStyleSheet(f"QListWidget {{ border-right: 1px solid {BORDER}; }}")
        self.list.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._apply_view_mode()
        split.addWidget(self.list, 5)

        self.detail = DetailPanel()
        split.addWidget(self.detail, 4)
        root.addLayout(split, 1)

        self.statusBar().setStyleSheet(f"color:{MUTED}; background:{BG};")

        # Global search shortcuts Ctrl+K and Ctrl+F
        act_search = QAction(self)
        act_search.setShortcuts(["Ctrl+K", "Ctrl+F"])
        act_search.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(act_search)

        # track resizes on the central widget so the overlay always covers it
        self.centralWidget().installEventFilter(self)

        # ---- overlay (must exist before any long op can start) ----
        self._build_overlay()

        # ---- search state & worker ----
        self._search_id = 0
        self._search_worker = None

        # ---- wiring ----
        self.search.textChanged.connect(self._debounce_search)
        self.search.returnPressed.connect(self.do_search)
        for cb in (self.pipeline, self.category, self.sort_by):
            cb.currentIndexChanged.connect(self.do_search)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.do_search)

        self._rendered_count = 0
        self._batch_size = 100
        self._is_rendering = False

        self.refresh_categories()
        self.refresh_stats()
        self.do_search()

        # Graceful shutdown handler on application quit
        QApplication.instance().aboutToQuit.connect(self._shutdown)

        # auto disk-scan at startup (background), then refresh
        self._long_op(lambda ev: local_scan.scan_all(), "Startup disk scan")

        # Check for latest GitHub release in background (non-blocking)
        QTimer.singleShot(2500, self._check_for_updates)

    # ---------------- search & display ----------------

    def _on_engine_chip(self, eng: Optional[str]):
        self.selected_engine = eng
        self.do_search()

    def _on_local_chip_toggled(self, checked: bool):
        self.local_only = checked
        self.do_search()

    def _open_sync_dialog(self):
        dlg = SyncDialog(self, parent=self)
        dlg.exec()

    def set_view_mode(self, mode: str):
        if mode not in ("list", "grid") or mode == self.view_mode:
            return
        self.view_mode = mode
        try:
            save_config_partial({"desktop_view_mode": mode})
        except Exception:
            pass
        self._apply_view_mode()
        self.list.clear()
        self._rendered_count = 0
        self._render_next_batch()

    def _apply_view_mode(self):
        if self.view_mode == "grid":
            self.btn_view_grid.setChecked(True)
            self.list.setViewMode(QListView.ViewMode.IconMode)
            self.list.setResizeMode(QListView.ResizeMode.Adjust)
            self.list.setGridSize(QSize(208, 228))
            self.list.setSpacing(8)
            self.list.setWordWrap(True)
        else:
            self.btn_view_list.setChecked(True)
            self.list.setViewMode(QListView.ViewMode.ListMode)
            self.list.setResizeMode(QListView.ResizeMode.Fixed)
            self.list.setGridSize(QSize())
            self.list.setSpacing(2)
            self.list.setWordWrap(False)

    def _debounce_search(self):
        self.search_timer.start()

    def do_search(self):
        self._search_id += 1
        q = self.search.text().strip()
        eng = self.selected_engine
        pipe = {0: None, 1: "HDRP", 2: "URP", 3: "Built-in"}[self.pipeline.currentIndex()]
        cat = None if self.category.currentIndex() <= 0 else self.category.currentText()
        sort_mode = {0: "relevance", 1: "title_asc", 2: "title_desc", 3: "claimed_desc", 4: "size_desc"}[self.sort_by.currentIndex()]

        # Show searching status in status bar
        self.statusBar().showMessage(f"Searching for '{q}'…" if q else "Browsing vault…")

        # Launch background SearchWorker
        worker = SearchWorker(self._search_id, q, eng, pipe, cat, sort_mode, local_only=self.local_only, parent=self)
        worker.results_ready.connect(self._on_search_results)
        self._search_worker = worker
        worker.start()

    def _on_search_results(self, query_id: int, items: list, mode: str):
        if query_id != self._search_id:
            return  # Stale search query result; ignore
        self.results = items
        self.list.clear()
        self._rendered_count = 0
        self._render_next_batch()

    def _render_next_batch(self):
        if getattr(self, "_is_rendering", False):
            return
        if not hasattr(self, "results") or self._rendered_count >= len(self.results):
            return
        self._is_rendering = True
        try:
            start = self._rendered_count
            end = min(start + self._batch_size, len(self.results))
            next_items = self.results[start:end]
            self._rendered_count = end  # Advance counter before mutating UI/scrollbar

            self.list.setUpdatesEnabled(False)
            try:
                for item in next_items:
                    li = QListWidgetItem(self.list)
                    li.setData(Qt.ItemDataRole.UserRole, item)
                    if self.view_mode == "grid":
                        card = AssetGridCard(item)
                    else:
                        card = AssetCard(item)
                    li.setSizeHint(card.sizeHint())
                    self.list.setItemWidget(li, card)
            finally:
                self.list.setUpdatesEnabled(True)

            total = len(self.results)
            if total > self._rendered_count:
                self.statusBar().showMessage(f"Showing {self._rendered_count} of {total} matches (scroll down for more)")
            else:
                self.statusBar().showMessage(f"{total} matches")
        finally:
            self._is_rendering = False

    def _on_scroll(self, value):
        if getattr(self, "_is_rendering", False):
            return
        sb = self.list.verticalScrollBar()
        if sb.maximum() - value < 150:
            self._render_next_batch()

    def _on_select(self, li: Optional[QListWidgetItem]):
        if not li:
            return
        item = li.data(Qt.ItemDataRole.UserRole)
        if not item:
            return
        full = get_asset_by_id(item["id"]) or item
        self.detail.show_asset(full)

    def refresh_categories(self):
        cats = get_categories()
        for c in cats:
            self.category.addItem(c)

    def refresh_stats(self):
        st = get_stats()
        from .db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM assets WHERE local_path != ''")
        n_local = cur.fetchone()[0]
        conn.close()
        srcs = " · ".join(f"{k}: {v}" for k, v in (st.get("sources") or {}).items())
        self.stats_label.setText(f"{st['total']} assets · {srcs} · ⚡{n_local} local")
        self.search.setPlaceholderText(f"Search {st['total']}+ assets… (Ctrl+K)")

    # ---------------- long operations ----------------

    # ---------------- overlay (greyed-out loading screen) ----------------

    def _build_overlay(self):
        self.overlay = QFrame(self.centralWidget())
        self.overlay.setStyleSheet("background: rgba(10, 14, 20, 235); border: none;")
        outer_lay = QVBoxLayout(self.overlay)
        outer_lay.setContentsMargins(20, 20, 20, 20)

        # Centered modal card container
        self.overlay_card = QFrame()
        self.overlay_card.setFixedWidth(540)
        self.overlay_card.setStyleSheet(f"""
            QFrame {{
                background: {PANEL};
                border: 1px solid {BORDER};
                border-radius: 12px;
            }}
        """)
        card_lay = QVBoxLayout(self.overlay_card)
        card_lay.setContentsMargins(28, 24, 28, 24)
        card_lay.setSpacing(14)

        self.overlay_icon = QLabel("⠋")
        self.overlay_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_icon.setStyleSheet(f"font-size: 32px; color: {ACCENT}; background: transparent; border: none; font-family: 'Segoe UI Symbol', 'Consolas';")

        self.overlay_title = QLabel("")
        self.overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_title.setWordWrap(True)
        self.overlay_title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {TEXT}; background: transparent; border: none;")

        self.overlay_status = QLabel("")
        self.overlay_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_status.setWordWrap(True)
        self.overlay_status.setStyleSheet(f"color: {MUTED}; font-size: 13px; line-height: 1.4; background: transparent; border: none;")

        self.overlay_bar = QProgressBar()
        self.overlay_bar.setFixedHeight(18)
        self.overlay_bar.setRange(0, 0)   # busy pulse until totals known
        self.overlay_bar.setTextVisible(True)
        self.overlay_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_bar.setFormat("%p%")
        self.overlay_bar.setStyleSheet(f"""
            QProgressBar {{
                background: #21262d;
                border: 1px solid {BORDER};
                border-radius: 6px;
                text-align: center;
                color: #ffffff;
                font-size: 11px;
                font-weight: 700;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {GREEN});
                border-radius: 5px;
            }}
        """)

        self.overlay_cancel = QPushButton("Cancel")
        self.overlay_cancel.setFixedWidth(110)
        self.overlay_cancel.setStyleSheet(f"""
            QPushButton {{
                background: #21262d; border: 1px solid {BORDER}; border-radius: 6px;
                color: {TEXT}; font-size: 12px; padding: 6px 12px;
            }}
            QPushButton:hover {{ background: #30363d; }}
        """)
        self.overlay_cancel.clicked.connect(self._cancel_op)

        card_lay.addWidget(self.overlay_icon)
        card_lay.addWidget(self.overlay_title)
        card_lay.addWidget(self.overlay_status)
        card_lay.addWidget(self.overlay_bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self.overlay_cancel)
        btn_row.addStretch()
        card_lay.addLayout(btn_row)

        outer_lay.addStretch()
        outer_lay.addWidget(self.overlay_card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer_lay.addStretch()

        self.overlay.hide()
        # braille spinner animation
        self._spin_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(120)
        self._spinner_timer.timeout.connect(self._spin_tick)

    def _spin_tick(self):
        self._spin_i = (self._spin_i + 1) % len(self._spin_frames)
        self.overlay_icon.setText(self._spin_frames[self._spin_i])

    def _show_overlay(self, title: str, status: str = "", cancellable: bool = True):
        self.overlay_title.setText(title)
        self.overlay_status.setText(status)
        self.overlay_status.setVisible(bool(status))
        self.overlay_bar.setRange(0, 0)   # busy until a total is known
        self.overlay_cancel.setVisible(cancellable)
        self.overlay.setGeometry(self.centralWidget().rect())
        self.overlay.raise_()
        self.overlay.show()
        QTimer.singleShot(0, lambda: self.overlay.setGeometry(self.centralWidget().rect()))
        self._op_t0 = time.time()
        self._spinner_timer.start()

    def _overlay_progress(self, *args, done=None, total=None, text=None):
        if len(args) == 3:
            done, total, text = args
        elif len(args) == 1:
            if isinstance(args[0], str):
                text = args[0]
        if not self.overlay.isHidden():
            if done is not None and total and total > 0:
                self.overlay_bar.setRange(0, total)
                self.overlay_bar.setValue(min(done, total))
                pct = int((min(done, total) / total) * 100)
                self.overlay_bar.setFormat(f"{pct}% ({done}/{total})")
                # ETA from observed rate
                elapsed = max(time.time() - getattr(self, "_op_t0", time.time()), 0.1)
                if done > 0 and done <= total:
                    eta = elapsed / done * (total - done)
                    if text:
                        text = f"{text}  ·  ~{int(eta // 60)}m {int(eta % 60):02d}s left"
            elif total == 0:
                self.overlay_bar.setRange(0, 0)
                self.overlay_bar.setFormat("Starting…")
            if text:
                self.overlay_status.setText(text)
        self.sync_status.setText(text or "")

    def _hide_overlay(self):
        self.overlay.hide()
        self._spinner_timer.stop()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "overlay"):
            self.overlay.setGeometry(self.centralWidget().rect())

    def eventFilter(self, obj, ev):
        # keep the overlay covering the content area through ANY layout change
        cw = getattr(self, "centralWidget", None)
        if cw and obj is cw() and ev.type() == QEvent.Type.Resize:
            if hasattr(self, "overlay"):
                self.overlay.setGeometry(cw().rect())
        elif hasattr(self, "search") and obj is self.search and ev.type() == QEvent.Type.KeyPress:
            if ev.key() == Qt.Key.Key_Down:
                if hasattr(self, "list") and self.list.count() > 0:
                    if self.list.currentRow() < 0:
                        self.list.setCurrentRow(0)
                    self.list.setFocus()
                    return True
            elif ev.key() == Qt.Key.Key_Escape:
                if self.search.text():
                    self.search.clear()
                    return True
                else:
                    self.hide()
                    return True
        elif hasattr(self, "list") and obj is self.list and ev.type() == QEvent.Type.KeyPress:
            if ev.key() == Qt.Key.Key_Space:
                curr = self.list.currentItem()
                if curr:
                    item = curr.data(Qt.ItemDataRole.UserRole)
                    if item:
                        full = get_asset_by_id(item["id"]) or item
                        ql = QuickLookDialog(full, self)
                        ql.exec()
                        return True
            elif ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                curr = self.list.currentItem()
                if curr:
                    self._on_double_click(curr)
                    return True
            elif ev.key() == Qt.Key.Key_Up and self.list.currentRow() <= 0:
                if hasattr(self, "search"):
                    self.search.setFocus()
                return True
            elif ev.key() == Qt.Key.Key_Escape:
                if hasattr(self, "search") and self.search.text():
                    self.search.clear()
                    self.search.setFocus()
                    return True
                else:
                    self.hide()
                    return True
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            if self.search.text():
                self.search.clear()
                self.search.setFocus()
                return
            else:
                self.hide()
                return
        super().keyPressEvent(ev)

    def _on_double_click(self, li: QListWidgetItem):
        item = li.data(Qt.ItemDataRole.UserRole)
        if not item:
            return
        local_path = item.get("local_path")
        if local_path and os.path.exists(local_path):
            target_dir = os.path.dirname(local_path) if os.path.isfile(local_path) else local_path
            if sys.platform == "win32":
                os.startfile(target_dir)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", target_dir])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", target_dir])
            return
        store_url = item.get("store_url")
        if store_url:
            webbrowser.open(store_url)

    def _on_update_available(self, version: str, url: str):
        self.update_btn.setText(f"✨ {version} available")
        self.update_btn.clicked.connect(lambda: webbrowser.open(url))
        self.update_btn.setVisible(True)

    def _check_for_updates(self):
        self._update_worker = UpdateChecker(parent=self)
        self._update_worker.update_available.connect(self._on_update_available)
        self._update_worker.start()

    def _cancel_op(self):
        if getattr(self, "_cancel_event", None):
            self._cancel_event.set()
            self.overlay_status.setText("Cancelling — finishing current item…")

    def _long_op(self, fn_builder, label, pre_status=None, success_text=None,
                 with_progress=False, cancellable=True, refresh_after=True,
                 auto_follow=None):
        """Run a blocking operation off-thread with the greyed-out overlay.
          fn_builder(cancel_event) -> result   (built here so ops get the event)
          auto_follow(msg, ok)     -> optionally chain another op on success
        """
        import threading as _threading
        # single-flight: two ops would fight over the same browser profile
        if getattr(self, "_op_running", False):
            self.statusBar().showMessage("⚠ Another operation is already running — wait for it to finish.", 4000)
            self.sync_status.setText("⚠ Another operation is already running — wait for it to finish.")
            return
        self._op_running = True
        self.sync_panel.setEnabled(False)
        self.sync_btn.setEnabled(False)
        self._cancel_event = _threading.Event()

        self.sync_status.setText(f"{label}…")
        self._show_overlay(f"{label}…", status=pre_status or "",
                           cancellable=cancellable and label != "Disk scan")

        def _runner(prog_cb):
            import inspect
            try:
                sig = inspect.signature(fn_builder)
                p_count = len(sig.parameters)
            except (ValueError, TypeError):
                p_count = 0

            if p_count >= 2:
                return fn_builder(self._cancel_event, prog_cb)
            elif p_count == 1:
                return fn_builder(self._cancel_event)
            else:
                return fn_builder()

        self.op = LongOp(_runner, label, success_text=success_text)
        if with_progress:
            self.op.progress.connect(
                lambda d, t, s: self._overlay_progress(done=d, total=t, text=s))

        def finished(msg, ok):
            self._op_running = False
            self.sync_panel.setEnabled(True)
            self.sync_btn.setEnabled(True)
            self._hide_overlay()
            self.sync_status.setText(msg)
            self.statusBar().showMessage(msg, 5000)
            cancelled = msg.startswith("Operation cancelled") or "cancelled" in msg.lower()
            if ok and not cancelled:
                if refresh_after:
                    self.do_search()
                    self.refresh_stats()
                if auto_follow:
                    QTimer.singleShot(100, lambda: auto_follow(msg, ok))

        self.op.done.connect(finished)
        self.op.start()

    def _fab_deep_pending(self) -> int:
        """Fab listings the browser pass can still improve (galleries or
        descriptions)."""
        return store_client.fab_deep_media_pending()

    def _start_enrich_sweep(self):
        pending = store_client.count_unenriched()
        fab_pending = self._fab_deep_pending()
        if pending == 0 and fab_pending == 0:
            self.sync_status.setText("Vault already fully enriched.")
            return

        def auto_fab(msg, ok):
            # Phase 2: after HTTP enrichment, sweep Fab listings in the
            # authed browser for their full screenshot galleries.
            left = self._fab_deep_pending()
            if ok and left > 0:
                self._long_op(
                    lambda ev, cb: store_client.run_fab_deep_media(cancel_event=ev,
                                                                   progress=cb),
                    "Fab galleries",
                    pre_status=f"HTTP pass done — visiting {left} Fab listings for full galleries…",
                    with_progress=True)

        self._long_op(
            lambda ev, prog_cb: store_client.enrich_assets(progress=prog_cb,
                                                           cancel_event=ev),
            "Enrichment",
            pre_status=(f"Enriching {pending} assets in batches…"
                        + (f" then {fab_pending} Fab listing visits." if fab_pending else "")),
            with_progress=True,
            auto_follow=auto_fab)

    def _fetch_op(self, provider):
        if not store_client.has_saved_session(provider):
            QMessageBox.information(self, "No saved login",
                                    f"No saved session for {provider}.\n"
                                    f"A login window will open — sign in once.")

        def auto_enrich(msg, ok):
            pending = store_client.count_unenriched()
            if ok and pending > 0:
                self._long_op(
                    lambda ev, prog_cb: store_client.enrich_assets(
                        progress=prog_cb, cancel_event=ev),
                    "Auto-enrichment",
                    pre_status=f"Fetch done — enriching {pending} new/updated assets in batches…",
                    with_progress=True)

        self._long_op(
            lambda ev, _cb: store_client.fetch_library(provider, cancel_event=ev),
            f"Fetch {provider}",
            pre_status=f"Fetching {provider} library — a browser window will open; leave it alone until it closes itself.",
            auto_follow=auto_enrich)

    # ---------------- tray & hotkey ----------------

    def make_tray_icon(self) -> Optional[QSystemTrayIcon]:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        pm = QPixmap(64, 64); pm.fill(QColor(BG))
        p = QPainter(pm); p.setPen(QColor(ACCENT))
        f = QFont("Segoe UI", 34, QFont.Weight.Bold); p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "V"); p.end()

        self._tray_menu = QMenu(self)
        act_show = QAction("Show / Hide", self)
        act_show.triggered.connect(self.toggle_visible)
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(QApplication.instance().quit)
        self._tray_menu.addAction(act_show); self._tray_menu.addSeparator(); self._tray_menu.addAction(act_quit)

        tray = QSystemTrayIcon(QIcon(pm), self)
        tray.setToolTip(f"Quartermaster v{__version__}")
        tray.setContextMenu(self._tray_menu)
        tray.activated.connect(
            lambda r: self.toggle_visible() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        return tray

    def toggle_visible(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.search.setFocus()

    def _shutdown(self):
        """Gracefully wait for any running background QThread before application teardown."""
        if getattr(self, "_cancel_event", None):
            self._cancel_event.set()
        op = getattr(self, "op", None)
        if op is not None and op.isRunning():
            op.wait(5000)
        sw = getattr(self, "_search_worker", None)
        if sw is not None and sw.isRunning():
            sw.wait(2000)
        uw = getattr(self, "_update_worker", None)
        if uw is not None and uw.isRunning():
            uw.wait(2000)

    def closeEvent(self, ev):
        # minimize-to-tray instead of quitting if tray is available (Quit is in the tray menu / Ctrl+Q)
        if getattr(self, "_tray", None) and QSystemTrayIcon.isSystemTrayAvailable() and \
                getattr(self, "_really_quit", False) is not True and \
                QApplication.instance().property("quitting") is not True:
            ev.ignore()
            self.hide()
            self._tray.showMessage("Quartermaster", "Still running in the tray — Win+Alt+V to reopen.",
                                   QSystemTrayIcon.MessageIcon.Information, 2500)
            return
        self._shutdown()
        ev.accept()


def _install_crash_logger():
    """All uncaught exceptions (main + threads) go to data/crash.log."""
    log_path = CRASH_LOG_PATH
    rotate_log_if_large(log_path)

    def _write(header, exc):
        import traceback
        import datetime
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.datetime.now().isoformat()} — {header} ===\n")
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        except Exception:
            pass

    def sys_hook(t, v, tb):
        _write("uncaught exception", v)
        sys.__stderr__.write(f"Quartermaster crashed — see {log_path}\n")

    def thread_hook(args):
        _write(f"exception in thread {args.thread.name}", args.exc)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


def main():
    _install_crash_logger()
    init_db()
    
    # Register explicit Windows Application ID for native Taskbar Icon grouping
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Quartermaster.AssetVault.Desktop.1.0")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)

    # ---- Single Instance Enforcement ----
    server_name = "Quartermaster_Desktop_App_SingleInstance"
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(400):
        # An instance is already running -> tell it to show/activate and exit cleanly
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
        socket.write(b"ACTIVATE\n")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.close()
        print("Quartermaster is already running — activating existing window.")
        time.sleep(1.0)
        sys.exit(0)

    # Clean up any stale pipe/server from previous crashed sessions
    QLocalServer.removeServer(server_name)
    local_server = QLocalServer()
    local_server.listen(server_name)

    # Set Window and Taskbar Icon
    app_icon = None
    if os.path.exists(ICON_PATH):
        app_icon = QIcon(ICON_PATH)
    elif os.path.exists(ICON_PNG):
        app_icon = QIcon(ICON_PNG)
        
    if app_icon:
        app.setWindowIcon(app_icon)

    win = MainWindow()
    if app_icon:
        win.setWindowIcon(app_icon)
    win._tray = win.make_tray_icon()
    win.show()

    def _activate_window():
        win.showNormal()
        win.raise_()
        win.activateWindow()
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(win.winId())
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

    def _on_new_connection():
        client = local_server.nextPendingConnection()
        if client:
            _activate_window()
            try:
                client.close()
            except Exception:
                pass

    local_server.newConnection.connect(_on_new_connection)

    hotkey = WinHotkeyFilter(win.toggle_visible)
    hotkey.install()
    app.installNativeEventFilter(hotkey)

    about = QAction("Quit", app)
    about.setShortcut("Ctrl+Q")
    about.triggered.connect(app.quit)
    win.addAction(about)

    if hotkey.registered:
        win.statusBar().showMessage("Global hotkey ready: Win+Alt+V", 5000)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
