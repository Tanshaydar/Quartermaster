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
import sys
import threading
from typing import Dict, List, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
)

try:
    from .db import search_assets, get_asset_by_id, get_stats, get_categories
    from .config import load_config
    from . import store_client, local_scan
except ImportError:
    from db import search_assets, get_asset_by_id, get_stats, get_categories
    from config import load_config
    import store_client, local_scan

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    padding: 7px 14px; color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QListWidget {{
    background: {BG}; border: none; outline: none;
}}
QListWidget::item {{ margin: 4px 8px; border-radius: 8px; }}
QListWidget::item:selected {{ background: {CARD}; border: 1px solid {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
a {{ color: {ACCENT}; }}
"""


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class LongOp(QThread):
    """Runs a blocking backend call off the UI thread."""
    done = Signal(str, bool)

    def __init__(self, fn, label, parent=None):
        super().__init__(parent)
        self.fn, self.label = fn, label

    def run(self):
        try:
            result = self.fn()
            msg = self.label
            if isinstance(result, dict) and "matched_to_library" in result:
                msg += f" — {result['matched_to_library']} on disk"
            elif isinstance(result, int):
                msg += f" — {result}"
            self.done.emit(msg + " ✓", True)
        except Exception as e:
            self.done.emit(f"{self.label} failed: {e}", False)


class _ImgSignals(QObject):
    fetched = Signal(str, QPixmap)


class ImageLoader(QRunnable):
    """Downloads (or loads from disk cache) an image and emits a QPixmap."""
    _signals: Dict[str, _ImgSignals] = {}

    def __init__(self, url: str, key: str):
        super().__init__()
        self.url, self.key = url, key
        if key not in ImageLoader._signals:
            ImageLoader._signals[key] = _ImgSignals()
        self.signals = ImageLoader._signals[key]

    @classmethod
    def cached_path(cls, cfg, url: str) -> str:
        import hashlib
        d = cfg["media_cache_dir"]
        os.makedirs(d, exist_ok=True)
        ext = ".png" if ".png" in url.lower() else ".jpg"
        return os.path.join(d, hashlib.sha1(url.encode()).hexdigest() + ext)

    def run(self):
        cfg = load_config()
        path = ImageLoader.cached_path(cfg, self.url) if cfg["media_cache_enabled"] else None
        try:
            if path and os.path.exists(path):
                data = open(path, "rb").read()
            else:
                import httpx
                r = httpx.get(self.url, timeout=15, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0", "Referer": self.url})
                r.raise_for_status()
                data = r.content
                if path:
                    with open(path, "wb") as f:
                        f.write(data)
            pm = QPixmap()
            if pm.loadFromData(data):
                self.signals.fetched.emit(self.key, pm)
        except Exception:
            pass


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
                      f"padding:1px 7px; font-size:11px;")
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lbl


class AssetCard(QWidget):
    """Compact list card: title / publisher+engine / category+local badges."""

    def __init__(self, item: dict):
        super().__init__()
        self.asset = item
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        title = QLabel(item["title"])
        title.setStyleSheet(f"font-weight:600; font-size:14px; color:{TEXT};")
        title.setWordWrap(True)

        meta = QHBoxLayout()
        pub = QLabel(item.get("publisher") or "")
        pub.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        eng = badge("Unity" if item["source"] == "unity" else "Fab",
                    "#7c9c47" if item["source"] == "unity" else "#c7c7c7")
        meta.addWidget(pub); meta.addStretch(); meta.addWidget(eng)

        row2 = QHBoxLayout()
        cat = badge(item.get("category", ""), "#a371f7")
        row2.addWidget(cat)
        if item.get("local_path"):
            loc = badge("⚡ Local", GREEN)
            loc.setToolTip(item["local_path"])
            row2.addWidget(loc)
        if item.get("size_str"):
            sz = QLabel(item["size_str"])
            sz.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            row2.addWidget(sz)
        row2.addStretch()

        for w in (title, *meta.children(), *row2.children()):
            if isinstance(w, QLabel):
                w.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        lay.addWidget(title)
        lay.addLayout(meta)
        lay.addLayout(row2)


class DetailPanel(QScrollArea):
    """Right-hand asset detail view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.current: Optional[dict] = None

        inner = QWidget()
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(18, 14, 18, 14)
        self.lay.setSpacing(10)
        self.setWidget(inner)
        self.show_placeholder()

    def clear_layout(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def show_placeholder(self):
        self.clear_layout(self.lay)
        lbl = QLabel("Select an asset to inspect it.\n\nWin+Alt+V shows/hides this window.")
        lbl.setStyleSheet(f"color:{MUTED}; font-size:14px;")
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

        cover = QLabel("  loading cover…  ")
        cover.setFixedHeight(190)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setStyleSheet(f"background:{CARD}; border-radius:8px; color:{MUTED};")
        self.lay.addWidget(cover)
        if item.get("image_url"):
            loader = ImageLoader(item["image_url"], f"cover_{item['id']}")
            loader.signals.fetched.connect(
                lambda _k, pm, c=cover: self._set_cover(c, pm))
            QThreadPool.globalInstance().start(loader)

        title = QLabel(f"<h2 style='margin:0'>{item['title']}</h2>")
        title.setWordWrap(True)
        self.lay.addWidget(title)

        eng = "Unity Asset Store" if item["source"] == "unity" else "Fab (Unreal)"
        sub = f"{item.get('publisher') or '—'} · {eng}"
        if item.get("version"):
            sub += f" · v{item['version']}"
        if item.get("size_str"):
            sub += f" · {item['size_str']}"
        subl = QLabel(sub)
        subl.setStyleSheet(f"color:{MUTED};")
        subl.setWordWrap(True)
        self.lay.addWidget(subl)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(3)

        def kv(k, v):
            kl = QLabel(k); kl.setStyleSheet(f"color:{MUTED};")
            vl = QLabel(v); vl.setWordWrap(True)
            vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
        local = item.get("local_path")
        kv("On disk", ("⚡ " + local) if local else "☁ cloud only")
        self.lay.addLayout(form)

        if item.get("summary"):
            _, b = self.section("About"); b.setText(item["summary"])
        if item.get("usage_notes"):
            _, b = self.section("Usage notes"); b.setText(item["usage_notes"])
        if item.get("tags"):
            _, b = self.section("Tags")
            b.setText("  ".join("#" + t for t in item["tags"]))

        videos = [v for v in (item.get("video_links") or [])]
        if videos:
            _, b = self.section("Videos")
            b.setOpenExternalLinks(True)
            b.setText("<br>".join(f"<a href='{v}'>{v}</a>" for v in videos))

        gallery = item.get("gallery_images") or []
        if gallery:
            head = QLabel("GALLERY")
            head.setStyleSheet(f"color:{ACCENT}; font-size:11px; font-weight:700;")
            self.lay.addWidget(head)
            row = QHBoxLayout()
            row.setSpacing(6)
            for i, g in enumerate(gallery[:6]):
                gl = QLabel("…")
                gl.setFixedSize(120, 68)
                gl.setStyleSheet(f"background:{CARD}; border-radius:6px;")
                loader = ImageLoader(g, f"g_{item['id']}_{i}")
                loader.signals.fetched.connect(lambda _k, pm, l=gl: self._set_thumb(l, pm))
                QThreadPool.globalInstance().start(loader)
                row.addWidget(gl)
            row.addStretch()
            self.lay.addLayout(row)

        if item.get("store_url"):
            link = QLabel(f"<a href='{item['store_url']}'>Open store listing ↗</a>")
            link.setOpenExternalLinks(True)
            self.lay.addWidget(link)

        if (item.get("local_path") or "").lower().endswith(".unitypackage"):
            unpack_btn = QPushButton("📥 Unpack into Unity project…")
            unpack_btn.clicked.connect(self._unpack_to_project)
            self.lay.addWidget(unpack_btn)

        copy_btn = QPushButton("📋 Copy context for AI")
        copy_btn.clicked.connect(self._copy_context)
        self.lay.addWidget(copy_btn)

        self.lay.addStretch()

    def _unpack_to_project(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from .unpacker import import_asset_to_project
        a = self.current
        proj = QFileDialog.getExistingDirectory(
            self, "Choose Unity project root", "",
            QFileDialog.Option.ShowDirsOnly)
        if not proj:
            return
        try:
            r = import_asset_to_project(a["id"], proj)
            QMessageBox.information(
                self, "Imported",
                f"Unpacked '{r['title']}' into:\n{r['project']}\n\n"
                f"Files written: {r['written']}  ·  skipped: {r['skipped']}")
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))

    @staticmethod
    def _set_cover(label: QLabel, pm: QPixmap):
        label.setPixmap(pm.scaled(label.width(), label.height(),
                                  Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                  Qt.TransformationMode.SmoothTransformation))

    @staticmethod
    def _set_thumb(label: QLabel, pm: QPixmap):
        label.setPixmap(pm.scaled(label.size(),
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation))

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
        self.setWindowTitle("VaultMCP")
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

        brand = QHBoxLayout()
        logo = QLabel("Vault<span style='color:%s'>MCP</span>" % ACCENT)
        logo.setTextFormat(Qt.TextFormat.RichText)
        logo.setStyleSheet("font-size:19px; font-weight:700;")
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(f"color:{MUTED};")
        brand.addWidget(logo); brand.addStretch(); brand.addWidget(self.stats_label)
        tl.addLayout(brand)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your vault…  (title, publisher, tag, notes)")
        self.engine = QComboBox(); self.engine.addItems(["All Engines", "Unity", "Fab / Unreal"])
        self.pipeline = QComboBox(); self.pipeline.addItems(["All Pipelines", "HDRP", "URP", "Built-in"])
        self.category = QComboBox(); self.category.addItem("All Categories")
        self.sync_btn = QPushButton("⚙ Sync")
        for w in (self.search, self.engine, self.pipeline, self.category, self.sync_btn):
            bar.addWidget(w)
        bar.setStretch(0, 1)
        tl.addLayout(bar)

        # ---- sync panel (hidden by default) ----
        self.sync_panel = QWidget()
        self.sync_panel.setVisible(False)
        sp = QHBoxLayout(self.sync_panel)
        sp.setContentsMargins(16, 8, 16, 8)
        sp.setSpacing(8)
        self.sync_status = QLabel("Log in with your own accounts; sessions stay on this machine.")
        self.sync_status.setStyleSheet(f"color:{MUTED};")

        def add_sync_btn(text, fn):
            b = QPushButton(text)
            b.clicked.connect(fn)
            sp.addWidget(b)
            return b

        add_sync_btn("🔐 Login Unity", lambda: self._long_op(
            lambda: store_client.interactive_login("unity"), "Unity login",
            pre_status="Browser opening… sign in with your Unity account, then CLOSE the window when done."))
        add_sync_btn("⟳ Fetch Unity", lambda: self._fetch_op("unity"))
        add_sync_btn("🔐 Login Fab", lambda: self._long_op(
            lambda: store_client.interactive_login("fab"), "Fab login",
            pre_status="Browser opening… sign in with your Epic account (complete the captcha), then CLOSE the window."))
        add_sync_btn("⟳ Fetch Fab", lambda: self._fetch_op("fab"))
        add_sync_btn("🖼 Enrich batch", lambda: self._long_op(
            lambda: store_client.enrich_assets(None), "Enrichment"))
        add_sync_btn("⚡ Scan local", lambda: self._long_op(local_scan.scan_all, "Disk scan"))
        sp.addWidget(self.sync_status, 1)
        tl.addWidget(self.sync_panel)

        root.addWidget(top)

        # ---- splitter: results | detail ----
        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.itemClicked.connect(self._on_select)
        self.list.setStyleSheet(f"QListWidget {{ border-right: 1px solid {BORDER}; }}")
        split.addWidget(self.list, 5)

        self.detail = DetailPanel()
        split.addWidget(self.detail, 4)
        root.addLayout(split, 1)

        self.statusBar().setStyleSheet(f"color:{MUTED}; background:{BG};")

        # ---- wiring ----
        self.search.textChanged.connect(self._debounce_search)
        for cb in (self.engine, self.pipeline, self.category):
            cb.currentIndexChanged.connect(self.do_search)
        self.sync_btn.clicked.connect(
            lambda: self.sync_panel.setVisible(not self.sync_panel.isVisible()))

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(150)
        self.search_timer.timeout.connect(self.do_search)

        self.refresh_categories()
        self.refresh_stats()
        self.do_search()

        # auto disk-scan at startup (background), then refresh
        self._long_op(local_scan.scan_all, "Startup disk scan", refresh_after=True)

    # ---------------- search & display ----------------

    def _debounce_search(self):
        self.search_timer.start()

    def do_search(self):
        q = self.search.text().strip()
        eng = {0: None, 1: "unity", 2: "fab"}[self.engine.currentIndex()]
        pipe = {0: None, 1: "HDRP", 2: "URP", 3: "Built-in"}[self.pipeline.currentIndex()]
        cat = None if self.category.currentIndex() <= 0 else self.category.currentText()
        self.results = search_assets(query=q or None, source=eng, pipeline=pipe,
                                     category=cat, limit=2000)
        self.list.clear()
        shown = self.results[:300]
        for item in shown:
            li = QListWidgetItem(self.list)
            li.setData(Qt.ItemDataRole.UserRole, item)
            li.setSizeHint(AssetCard(item).sizeHint())
            self.list.setItemWidget(li, AssetCard(item))
        total = len(self.results)
        note = "" if total <= 300 else f" (showing first 300)"
        self.statusBar().showMessage(f"{total} matches{note}")

    def _on_select(self, li: QListWidgetItem):
        item = li.data(Qt.ItemDataRole.UserRole)
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

    # ---------------- long operations ----------------

    def _long_op(self, fn, label, refresh_after=False, pre_status=None):
        if pre_status:
            self.sync_status.setText(pre_status)
        else:
            self.sync_status.setText(f"{label}…")
        self.op = LongOp(fn, label)

        def finished(msg, ok):
            self.sync_status.setText(msg)
            if refresh_after or True:
                self.do_search()
                self.refresh_stats()

        self.op.done.connect(finished)
        self.op.start()

    def _fetch_op(self, provider):
        if not store_client.has_saved_session(provider):
            QMessageBox.information(self, "No saved login",
                                    f"No saved session for {provider}.\n"
                                    f"A login window will open — sign in once.")
        self._long_op(lambda: store_client.fetch_library(provider), f"Fetch {provider}")

    # ---------------- tray & hotkey ----------------

    def make_tray_icon(self) -> QSystemTrayIcon:
        pm = QPixmap(64, 64); pm.fill(QColor(BG))
        p = QPainter(pm); p.setPen(QColor(ACCENT))
        f = QFont("Segoe UI", 34, QFont.Weight.Bold); p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "V"); p.end()

        menu = QMenu()
        act_show = QAction("Show / Hide", self)
        act_show.triggered.connect(self.toggle_visible)
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(QApplication.instance().quit)
        menu.addAction(act_show); menu.addSeparator(); menu.addAction(act_quit)

        tray = QSystemTrayIcon(QIcon(pm), self)
        tray.setToolTip("VaultMCP")
        tray.setContextMenu(menu)
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

    def closeEvent(self, ev):
        # minimize-to-tray instead of quitting (Quit is in the tray menu / Ctrl+Q)
        if getattr(self, "_tray", None) and getattr(self, "_really_quit", False) is not True \
                and QApplication.instance().property("quitting") is not True:
            ev.ignore()
            self.hide()
            self._tray.showMessage("VaultMCP", "Still running in the tray — Win+Alt+V to reopen.",
                                   QSystemTrayIcon.MessageIcon.Information, 2500)
            return
        ev.accept()


def _install_crash_logger():
    """All uncaught exceptions (main + threads) go to data/crash.log."""
    log_path = os.path.join(ROOT_DIR, "data", "crash.log")

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
        sys.__stderr__.write(f"VaultMCP crashed — see {log_path}\n")

    def thread_hook(args):
        _write(f"exception in thread {args.thread.name}", args.exc)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


def main():
    _install_crash_logger()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)

    win = MainWindow()
    win._tray = win.make_tray_icon()
    win.show()

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
