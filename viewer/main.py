"""Restricted PDF viewer for pdf-drm.

Opens a .cpdf file, asks the license server for a decryption key bound to
this machine's hardware fingerprint, and renders pages as raster images.
There is deliberately no Print, Save, or Export menu item anywhere in this
app -- that omission *is* the print restriction. See README.md for the
limitations of this approach (screen capture, camera photos, etc. are not
and cannot be prevented by any PDF-based DRM).
"""
from __future__ import annotations

import gc
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF
import requests
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import crypto  # noqa: E402
from common.machine_id import get_machine_fingerprint  # noqa: E402

CONFIG_PATH = Path.home() / ".pdf_drm_viewer" / "config.json"
RECHECK_INTERVAL_MS = 5 * 60 * 1000  # re-validate license with server every 5 min
OFFLINE_GRACE_MS = 5 * 60 * 1000  # force-close if the server stays unreachable this long
OFFLINE_RETRY_MS = 30 * 1000  # retry cadence once offline, faster than the normal recheck
WATERMARK_TICK_MS = 1600  # how often the watermark jumps to a new position
WATERMARK_POSITIONS = [(0.2, 0.3), (0.5, 0.75)]  # fractional (x, y) -> 2 marks, spread diagonally
DPI = 150

ACCENT = "#b91c1c"  # matches the web admin dashboard's red accent
ACCENT_BG = "#fde8e8"
PAGE_MAT_COLOR = "#5f6368"  # backdrop behind the page, like most PDF readers use

APP_STYLESHEET = f"""
QMainWindow, QDialog {{
    background: #f5f5f5;
}}

QToolBar {{
    background: #fafafa;
    border: none;
    border-bottom: 1px solid #dcdcdc;
    padding: 5px 8px;
    spacing: 2px;
}}

QToolBar QToolButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    color: #333333;
    font-size: 13px;
}}

QToolBar QToolButton:hover {{
    background: rgba(0, 0, 0, 0.06);
}}

QToolBar QToolButton:pressed {{
    background: rgba(0, 0, 0, 0.12);
}}

QToolBar QToolButton:checked {{
    background: {ACCENT_BG};
    color: {ACCENT};
    font-weight: 600;
}}

QToolBar::separator {{
    background: #dcdcdc;
    width: 1px;
    margin: 6px 6px;
}}

QToolBar QLabel {{
    color: #555555;
    padding: 0 2px;
}}

QToolBar QLineEdit, QToolBar QSpinBox {{
    border: 1px solid #d0d0d0;
    border-radius: 5px;
    padding: 4px 8px;
    background: white;
    selection-background-color: {ACCENT_BG};
}}

QToolBar QLineEdit:focus, QToolBar QSpinBox:focus {{
    border: 1px solid {ACCENT};
}}

#searchStatus {{
    color: {ACCENT};
    font-weight: 600;
    padding: 0 8px;
}}

QLabel#pageLabel {{
    background: {PAGE_MAT_COLOR};
    color: #eeeeee;
    font-size: 15px;
}}

QDockWidget {{
    color: #333333;
    font-weight: 600;
    font-size: 13px;
}}

QDockWidget::title {{
    background: #fafafa;
    border-bottom: 1px solid #dcdcdc;
    padding: 8px;
}}

QTreeWidget {{
    background: white;
    border: none;
    outline: none;
    font-size: 13px;
    padding: 4px;
}}

QTreeWidget::item {{
    padding: 6px 4px;
    border-radius: 4px;
}}

QTreeWidget::item:selected {{
    background: {ACCENT_BG};
    color: {ACCENT};
}}

QTreeWidget::item:hover:!selected {{
    background: #f0f0f0;
}}

QStatusBar {{
    background: #fafafa;
    border-top: 1px solid #dcdcdc;
    color: #555555;
    font-size: 12px;
}}

QPushButton, QDialogButtonBox QPushButton {{
    padding: 6px 16px;
    border-radius: 6px;
    border: 1px solid #d0d0d0;
    background: white;
}}

QPushButton:hover {{
    background: #f5f5f5;
}}

QDialog QLineEdit {{
    border: 1px solid #d0d0d0;
    border-radius: 5px;
    padding: 6px 8px;
}}

QDialog QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
"""


def parse_iso(value: str) -> datetime:
    # Python 3.10's fromisoformat() can't parse a trailing "Z" (fixed in
    # 3.11+); normalize it so this works regardless of Python version.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_server_url_secure(url: str) -> bool:
    """HTTPS is required -- decryption keys travel over this connection on
    every open, and plain HTTP is trivially sniffable on any shared network
    (wifi, office LAN, etc). The one exception is localhost/127.0.0.1/::1,
    where the traffic never leaves the machine, so there's nothing on the
    wire to intercept -- keeps local development workable without
    weakening the real requirement for anything reachable over a network."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config))


class FirstRunDialog(QDialog):
    """Collects server URL + who's using this machine, shown once on first
    launch. The username/email are sent to the license server right away as
    a self-service access request, so the admin can approve this machine
    for whichever document(s) they choose without the user needing to know
    which document to ask for by name."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Secure PDF Viewer -- Setup")
        self.server_input = QLineEdit()
        self.username_input = QLineEdit()
        self.email_input = QLineEdit()

        form = QFormLayout()
        form.addRow("Server URL:", self.server_input)
        form.addRow("Your name:", self.username_input)
        form.addRow("Your email:", self.email_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, str]:
        return (
            self.server_input.text().strip(),
            self.username_input.text().strip(),
            self.email_input.text().strip(),
        )


class PagingScrollArea(QScrollArea):
    """A QScrollArea whose mouse wheel / trackpad scroll turns the page once
    you're at the top/bottom edge of the current one, instead of just
    stopping there -- lets you flip through a document with scroll
    gestures alone, no need to reach for the Prev/Next buttons."""

    def __init__(self, viewer: "Viewer") -> None:
        super().__init__()
        self._viewer = viewer

    def wheelEvent(self, event) -> None:
        bar = self.verticalScrollBar()
        delta = event.angleDelta().y()
        if delta < 0 and bar.value() >= bar.maximum():
            if self._viewer.next_page():
                bar.setValue(0)
                event.accept()
                return
        elif delta > 0 and bar.value() <= bar.minimum():
            if self._viewer.prev_page():
                bar.setValue(bar.maximum())
                event.accept()
                return
        super().wheelEvent(event)


class LicenseDenied(Exception):
    def __init__(self, doc_id: str, detail: str) -> None:
        super().__init__(detail)
        self.doc_id = doc_id
        self.detail = detail


class Viewer(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Secure PDF Viewer")
        self.resize(900, 1000)

        self.doc: fitz.Document | None = None
        self.doc_id: str | None = None
        self.expires_at: datetime | None = None
        self.page_index = 0
        self.zoom = 1.0
        self.fingerprint = get_machine_fingerprint()
        self.watermark_label = self.fingerprint[:12]
        self._raw_pixmap: QPixmap | None = None
        self._offline_since: datetime | None = None
        self._search_matches: list[tuple[int, fitz.Rect]] = []
        self._search_index = -1
        self._last_search_query = ""

        config = load_config()
        if not config.get("server_url") or not config.get("username") or not config.get("email"):
            config = self._first_run_setup()
        elif not is_server_url_secure(config["server_url"]):
            # Config on disk (or edited by hand) points at a non-HTTPS,
            # non-localhost server -- don't silently send decryption keys
            # over that. Make the user fix it before anything else runs.
            QMessageBox.warning(
                self, "Insecure server URL",
                f"The saved server URL ({config['server_url']}) does not use HTTPS.\n\n"
                "Traffic between this app and the server -- including your "
                "decryption keys -- would be sent unencrypted and could be "
                "intercepted on the network.\n\nPlease enter a secure (https://) "
                "server URL.",
            )
            config = self._first_run_setup()
        self.server_url = config["server_url"]
        self.username = config["username"]
        self.email = config["email"]

        self._build_ui()

        self.recheck_timer = QTimer(self)
        self.recheck_timer.timeout.connect(self._revalidate_license)
        self.recheck_timer.start(RECHECK_INTERVAL_MS)

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_status)
        self.clock_timer.start(1000)

        self.watermark_timer = QTimer(self)
        self.watermark_timer.timeout.connect(self._tick_watermark)
        self.watermark_timer.start(WATERMARK_TICK_MS)

    def _first_run_setup(self) -> dict:
        while True:
            dialog = FirstRunDialog()
            if dialog.exec() != QDialog.Accepted:
                sys.exit(0)
            server_url, username, email = dialog.values()
            if not server_url or not username or not email:
                QMessageBox.critical(self, "Setup incomplete", "Server URL, name, and email are all required.")
                continue
            if not is_server_url_secure(server_url):
                QMessageBox.critical(
                    self, "Insecure server URL",
                    "Server URL must use HTTPS (e.g. https://your-server.com) so "
                    "traffic -- including your decryption keys -- can't be "
                    "intercepted on the network.\n\nPlain http:// is only allowed "
                    "for localhost, for local development.",
                )
                continue
            break

        config = {"server_url": server_url, "username": username, "email": email}
        save_config(config)
        self._register_this_machine(server_url, username, email)
        return config

    def _register_this_machine(self, server_url: str, username: str, email: str) -> None:
        try:
            resp = requests.post(
                f"{server_url.rstrip('/')}/request-access",
                json={
                    "machine_fingerprint": self.fingerprint,
                    "username": username,
                    "email": email,
                    "note": "First-run registration",
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            QMessageBox.warning(
                self, "Registration failed",
                f"Could not register this machine with the server: {exc}\n\n"
                "You can still open documents once you're granted access; if this "
                "keeps failing, check the server URL and your network connection.",
            )
            return
        req_id = resp.json().get("request_id")
        QMessageBox.information(
            self, "Registered",
            f"This machine has been registered with the server (request #{req_id}).\n\n"
            "Ask the document owner to approve your access -- once approved, "
            "you'll be able to open any document they grant you.",
        )

    def _build_ui(self) -> None:
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setObjectName("mainToolbar")
        self.addToolBar(toolbar)

        open_action = QAction("Open...", self)
        open_action.triggered.connect(self.open_file)
        toolbar.addAction(open_action)
        toolbar.addSeparator()

        prev_action = QAction("‹ Prev", self)
        prev_action.triggered.connect(self.prev_page)
        toolbar.addAction(prev_action)

        next_action = QAction("Next ›", self)
        next_action.triggered.connect(self.next_page)
        toolbar.addAction(next_action)
        toolbar.addSeparator()

        zoom_in = QAction("Zoom +", self)
        zoom_in.triggered.connect(lambda: self._set_zoom(self.zoom * 1.2))
        toolbar.addAction(zoom_in)

        zoom_out = QAction("Zoom −", self)
        zoom_out.triggered.connect(lambda: self._set_zoom(self.zoom / 1.2))
        toolbar.addAction(zoom_out)
        toolbar.addSeparator()

        toc_action = QAction("Bookmarks", self)
        toc_action.setCheckable(True)
        toc_action.setChecked(True)
        toc_action.toggled.connect(lambda checked: self.toc_dock.setVisible(checked))
        toolbar.addAction(toc_action)

        search_action = QAction("Search", self)
        search_action.setCheckable(True)
        search_action.setShortcut(QKeySequence.Find)
        search_action.toggled.connect(self._toggle_search_bar)
        toolbar.addAction(search_action)
        self.search_action = search_action
        toolbar.addSeparator()

        toolbar.addWidget(QLabel("Page "))
        self.goto_input = QSpinBox()
        self.goto_input.setMinimum(1)
        self.goto_input.setMaximum(1)
        self.goto_input.editingFinished.connect(self._goto_page_from_input)
        toolbar.addWidget(self.goto_input)

        # NOTE: intentionally no Print / Save / Export / Save As action exists
        # anywhere in this application.

        self.search_bar = QToolBar()
        self.search_bar.setObjectName("searchBar")
        self.search_bar.setMovable(False)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find in document...")
        self.search_input.setObjectName("searchInput")
        self.search_input.returnPressed.connect(self._on_search_enter)
        self.search_bar.addWidget(self.search_input)
        prev_match = QAction("Previous", self)
        prev_match.triggered.connect(self._search_prev)
        self.search_bar.addAction(prev_match)
        next_match = QAction("Next", self)
        next_match.triggered.connect(self._search_next)
        self.search_bar.addAction(next_match)
        self.search_status = QLabel("")
        self.search_status.setObjectName("searchStatus")
        self.search_bar.addWidget(self.search_status)
        close_search = QAction("Close", self)
        close_search.triggered.connect(lambda: self.search_action.setChecked(False))
        self.search_bar.addAction(close_search)
        self.addToolBar(Qt.TopToolBarArea, self.search_bar)
        self.search_bar.setVisible(False)

        self.label = QLabel("Open a .cpdf file to begin")
        self.label.setObjectName("pageLabel")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setAttribute(Qt.WA_StyledBackground, True)
        scroll = PagingScrollArea(self)
        scroll.setObjectName("pageScrollArea")
        scroll.setWidget(self.label)
        scroll.setWidgetResizable(True)
        scroll.viewport().setStyleSheet(f"background-color: {PAGE_MAT_COLOR};")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self.setCentralWidget(container)

        self.toc_tree = QTreeWidget()
        self.toc_tree.setHeaderHidden(True)
        self.toc_tree.itemClicked.connect(self._on_toc_item_clicked)
        self.toc_dock = QDockWidget("Bookmarks", self)
        self.toc_dock.setObjectName("bookmarksDock")
        self.toc_dock.setWidget(self.toc_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.toc_dock)

        self.setStatusBar(QStatusBar())

    # ---------- file open / license flow ----------

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open encrypted PDF", "", "Encrypted PDF (*.cpdf)")
        if not path:
            return
        try:
            self._load_encrypted(Path(path))
        except LicenseDenied as exc:
            self._offer_request_access(exc.doc_id, exc.detail)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Cannot open document", str(exc))

    def _offer_request_access(self, doc_id: str, detail: str) -> None:
        choice = QMessageBox.question(
            self,
            "Access denied",
            f"This computer is not licensed to open this document.\n\nServer said: {detail}\n\n"
            f"Send an access request as {self.username} <{self.email}> now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            return
        try:
            resp = requests.post(
                f"{self.server_url.rstrip('/')}/request-access",
                json={
                    "machine_fingerprint": self.fingerprint,
                    "username": self.username,
                    "email": self.email,
                    "note": doc_id,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            QMessageBox.critical(self, "Request failed", str(exc))
            return
        req_id = resp.json().get("request_id")
        QMessageBox.information(
            self, "Request sent",
            f"Access request #{req_id} sent. You'll be able to open the document once "
            "the owner approves it.",
        )

    def _load_encrypted(self, path: Path) -> None:
        blob = path.read_bytes()
        doc_id = crypto.read_doc_id(blob)

        resp = requests.post(
            f"{self.server_url.rstrip('/')}/license/request",
            json={"doc_id": doc_id, "machine_fingerprint": self.fingerprint},
            timeout=15,
        )
        if resp.status_code != 200:
            detail = resp.json().get("detail", resp.text) if resp.content else resp.text
            raise LicenseDenied(doc_id, str(detail))

        data = resp.json()
        key = bytes.fromhex(data["key_hex"])
        pdf_bytes = crypto.decrypt_pdf(blob, key)

        self.doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        self.doc_id = doc_id
        self.expires_at = parse_iso(data["expires_at"])
        self.title = data["title"]
        self.watermark_label = data.get("watermark_label") or self.fingerprint[:12]
        self.page_index = 0
        self._clear_search()
        self._offline_since = None
        self.recheck_timer.setInterval(RECHECK_INTERVAL_MS)

        # Best-effort: drop our reference to the plaintext bytes; PyMuPDF has
        # already copied what it needs into its own internal buffer.
        del pdf_bytes
        gc.collect()

        self.setWindowTitle(f"Secure PDF Viewer — {self.title}")
        self._populate_toc()
        self.goto_input.blockSignals(True)
        self.goto_input.setMaximum(len(self.doc))
        self.goto_input.setValue(1)
        self.goto_input.blockSignals(False)
        self._render_page()

    def _populate_toc(self) -> None:
        self.toc_tree.clear()
        if not self.doc:
            return
        toc = self.doc.get_toc(simple=True)  # list of [level, title, page_1_based]
        if not toc:
            placeholder = QTreeWidgetItem(["(No bookmarks in this document)"])
            placeholder.setFlags(Qt.ItemIsEnabled)
            self.toc_tree.addTopLevelItem(placeholder)
            return

        stack: list[tuple[int, QTreeWidgetItem]] = []  # (level, item)
        for level, title, page in toc:
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.UserRole, page)
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.toc_tree.addTopLevelItem(item)
            stack.append((level, item))
        self.toc_tree.expandAll()

    def _on_toc_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        page = item.data(0, Qt.UserRole)
        if not page or not self.doc:
            return
        self.page_index = max(0, min(len(self.doc) - 1, page - 1))
        self._render_page()

    def _revalidate_license(self) -> None:
        if not self.doc_id:
            return
        try:
            resp = requests.post(
                f"{self.server_url.rstrip('/')}/license/request",
                json={"doc_id": self.doc_id, "machine_fingerprint": self.fingerprint},
                timeout=15,
            )
        except requests.RequestException:
            self._handle_offline()
            return

        # Reachable again -- drop offline tracking and go back to the normal
        # (slower) recheck cadence.
        if self._offline_since is not None:
            self._offline_since = None
            self.recheck_timer.setInterval(RECHECK_INTERVAL_MS)

        if resp.status_code != 200:
            self._close_document("Your license for this document is no longer valid "
                                  "(revoked or expired). The document has been closed.")

    def _handle_offline(self) -> None:
        # A single unreachable check used to just warn and keep the document
        # open indefinitely -- an attacker (or just a flaky network) could
        # disconnect and the periodic re-check would never actually enforce
        # anything. Now: the first failure starts a bounded grace period and
        # switches to faster retries; if the server is still unreachable
        # once that grace period elapses, the document force-closes.
        now = datetime.now(timezone.utc)
        if self._offline_since is None:
            self._offline_since = now
            self.recheck_timer.setInterval(OFFLINE_RETRY_MS)
            QMessageBox.warning(
                self, "Offline",
                "Could not reach the license server to re-validate this document.\n\n"
                f"The document will close automatically in {OFFLINE_GRACE_MS // 60000} "
                "minutes if the connection isn't restored.",
            )
            return
        elapsed_ms = (now - self._offline_since).total_seconds() * 1000
        if elapsed_ms >= OFFLINE_GRACE_MS:
            self._close_document(
                "Could not reach the license server to re-validate this document "
                f"for over {OFFLINE_GRACE_MS // 60000} minutes. The document has been "
                "closed for your security."
            )

    def _close_document(self, message: str) -> None:
        self.doc = None
        self.doc_id = None
        self._raw_pixmap = None
        self._offline_since = None
        self.recheck_timer.setInterval(RECHECK_INTERVAL_MS)
        self._search_matches = []
        self._search_index = -1
        self._last_search_query = ""
        self.label.setText("")
        self.toc_tree.clear()
        self.setWindowTitle("Secure PDF Viewer")
        gc.collect()
        QMessageBox.warning(self, "Document closed", message)

    # ---------- rendering ----------

    def _render_page(self) -> None:
        if not self.doc:
            return
        # Render at DPI * screen device-pixel-ratio so the page is sharp on
        # HiDPI/Retina displays -- without this, a 150-DPI bitmap gets
        # upscaled by the OS to fill the screen's extra physical pixels,
        # which is what makes it look blurry compared to the source PDF.
        dpr = self.devicePixelRatioF() or 1.0
        page = self.doc[self.page_index]
        mat = fitz.Matrix(self.zoom * DPI * dpr / 72, self.zoom * DPI * dpr / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        self._raw_pixmap = QPixmap.fromImage(image.copy())
        self.goto_input.blockSignals(True)
        self.goto_input.setValue(self.page_index + 1)
        self.goto_input.blockSignals(False)
        self._tick_watermark()
        self._update_status()

    def _tick_watermark(self) -> None:
        # Re-composites the watermark at new random positions over the
        # cached (unwatermarked) page render. Runs on a timer so the marks
        # keep shifting on screen -- makes it harder to frame a photo that
        # avoids them, and the moving text draws the eye if someone is
        # filming/photographing the screen. Two marks (WATERMARK_POSITIONS),
        # spread diagonally and jittered, rather than a dense tile.
        if not self._raw_pixmap:
            return
        dpr = self.devicePixelRatioF() or 1.0
        result = QPixmap(self._raw_pixmap)
        painter = QPainter(result)

        # Highlight the active search match on this page, if any -- drawn
        # first so the watermark stays on top and legible.
        if 0 <= self._search_index < len(self._search_matches):
            match_page, match_rect = self._search_matches[self._search_index]
            if match_page == self.page_index:
                mat = fitz.Matrix(self.zoom * DPI * dpr / 72, self.zoom * DPI * dpr / 72)
                pixel_rect = match_rect * mat
                painter.setOpacity(0.5)
                painter.setBrush(QColor(255, 235, 0))
                painter.setPen(Qt.NoPen)
                painter.drawRect(pixel_rect.x0, pixel_rect.y0, pixel_rect.width, pixel_rect.height)

        painter.setOpacity(0.35)
        painter.setPen(QColor(220, 0, 0))
        font = painter.font()
        font.setPointSize(28)
        font.setBold(True)
        painter.setFont(font)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"{self.watermark_label}  {self.fingerprint[:16]}  {now}"

        w, h = result.width(), result.height()
        for fx, fy in WATERMARK_POSITIONS:
            cx = fx * w + random.randint(-int(w * 0.08), int(w * 0.08))
            cy = fy * h + random.randint(-int(h * 0.08), int(h * 0.08))
            angle = random.choice([-30, -20, -10, 10, 20, 30])
            painter.save()
            painter.translate(cx, cy)
            painter.rotate(angle)
            painter.drawText(0, 0, text)
            painter.restore()
        painter.end()
        # Tag the finished pixmap with the screen's device-pixel-ratio only
        # now (after all drawing, which stays in plain raw-pixel coordinates
        # throughout) so Qt displays the extra resolution crisply instead of
        # blowing the image up to a larger logical size.
        result.setDevicePixelRatio(dpr)
        self.label.setPixmap(result)

    def prev_page(self) -> bool:
        if self.doc and self.page_index > 0:
            self.page_index -= 1
            self._render_page()
            return True
        return False

    def next_page(self) -> bool:
        if self.doc and self.page_index < len(self.doc) - 1:
            self.page_index += 1
            self._render_page()
            return True
        return False

    def _set_zoom(self, value: float) -> None:
        self.zoom = max(0.25, min(4.0, value))
        self._render_page()

    def _goto_page_from_input(self) -> None:
        if not self.doc:
            return
        target = max(0, min(len(self.doc) - 1, self.goto_input.value() - 1))
        if target != self.page_index:
            self.page_index = target
            self._render_page()

    # ---------- search ----------

    def _toggle_search_bar(self, checked: bool) -> None:
        self.search_bar.setVisible(checked)
        if checked:
            self.search_input.setFocus()
            self.search_input.selectAll()
        else:
            self._clear_search()

    def _clear_search(self) -> None:
        self._search_matches = []
        self._search_index = -1
        self._last_search_query = ""
        self.search_status.setText("")
        self._tick_watermark()

    def _on_search_enter(self) -> None:
        query = self.search_input.text().strip()
        if query != self._last_search_query:
            self._last_search_query = query
            self._run_search(query)
        else:
            self._search_next()

    def _run_search(self, query: str) -> None:
        self._search_matches = []
        self._search_index = -1
        if not self.doc or not query:
            self.search_status.setText("")
            self._tick_watermark()
            return
        for i in range(len(self.doc)):
            for rect in self.doc[i].search_for(query):
                self._search_matches.append((i, rect))
        if not self._search_matches:
            self.search_status.setText("No matches")
            self._tick_watermark()
            return
        self._search_index = 0
        self._goto_match()

    def _goto_match(self) -> None:
        if not (0 <= self._search_index < len(self._search_matches)):
            return
        match_page, _ = self._search_matches[self._search_index]
        self.search_status.setText(f"{self._search_index + 1}/{len(self._search_matches)}")
        if self.page_index != match_page:
            self.page_index = match_page
            self._render_page()
        else:
            self._tick_watermark()

    def _search_next(self) -> None:
        if not self._search_matches:
            self._on_search_enter()
            return
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        self._goto_match()

    def _search_prev(self) -> None:
        if not self._search_matches:
            self._on_search_enter()
            return
        self._search_index = (self._search_index - 1) % len(self._search_matches)
        self._goto_match()

    # ---------- status ----------

    def _update_status(self) -> None:
        if not self.doc or not self.expires_at:
            self.statusBar().showMessage("No document open")
            return
        remaining = self.expires_at - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            self._close_document("Your license for this document has expired.")
            return
        days = remaining.days
        hours = remaining.seconds // 3600
        minutes = (remaining.seconds % 3600) // 60
        self.statusBar().showMessage(
            f"Page {self.page_index + 1}/{len(self.doc)}   |   "
            f"License valid for {days}d {hours}h {minutes}m   |   "
            f"Printing/export disabled"
        )


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    win = Viewer()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
