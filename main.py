import json
import socket
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QKeySequence, QScreen, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from pynput import keyboard

CORNER_POSITIONS = ("bottom-left", "bottom-right", "top-left", "top-right")
CONFIG_PATH = Path(__file__).parent / "config.json"
DEFAULT_CONFIG = {
    "overlay_width": 1280,
    "overlay_height": 720,
    "overlay_screen": "",
    "corner_enabled": True,
    "corner_size": 60,
    "corner_position": "bottom-left",
    "server_enabled": False,
    "server_port": 8765,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {**DEFAULT_CONFIG, **data}
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


class MouseSocketServer:
    """Minimal TCP broadcast server for mouse events."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self._host = host
        self._port = port
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self._server_socket: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(5)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        print(f"[socket] listening on {self._host}:{self._port}")

    def _accept_loop(self) -> None:
        assert self._server_socket
        while self._running:
            try:
                conn, addr = self._server_socket.accept()
                conn.setblocking(True)
                with self._clients_lock:
                    self._clients.append(conn)
                print(f"[socket] client connected: {addr}")
            except OSError:
                break

    def broadcast(self, payload: dict) -> None:
        if not self._running:
            return
        data = (json.dumps(payload) + "\n").encode("utf-8")
        stale: list[socket.socket] = []
        with self._clients_lock:
            for conn in list(self._clients):
                try:
                    conn.sendall(data)
                except OSError:
                    stale.append(conn)
            for conn in stale:
                try:
                    conn.close()
                except OSError:
                    pass
                if conn in self._clients:
                    self._clients.remove(conn)

    def stop(self) -> None:
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        with self._clients_lock:
            for conn in self._clients:
                try:
                    conn.close()
                except OSError:
                    pass
            self._clients = []
        print("[socket] stopped")


def _is_in_corner(geometry: QRect, x: float, y: float, threshold: int, position: str) -> bool:
    """Return True if (x, y) is inside the target corner hotzone."""
    tx = geometry.x()
    ty = geometry.y()
    w = geometry.width()
    h = geometry.height()

    if position == "bottom-left":
        return x <= tx + threshold and y >= ty + h - threshold
    if position == "bottom-right":
        return x >= tx + w - threshold and y >= ty + h - threshold
    if position == "top-left":
        return x <= tx + threshold and y <= ty + threshold
    if position == "top-right":
        return x >= tx + w - threshold and y <= ty + threshold
    # Fallback to bottom-left
    return x <= tx + threshold and y >= ty + h - threshold


def _post_to_gui(callback: Callable[[], None]) -> None:
    """Schedule a callback on the Qt GUI thread."""
    QTimer.singleShot(0, callback)


class HotkeyService:
    """Registers global shortcuts using pynput and forwards to Qt safely."""

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_quit: Optional[Callable[[], None]] = None,
        toggle_combo: str = "<ctrl>+<alt>+o",
        quit_combo: str = "<ctrl>+<alt>+q",
    ) -> None:
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._listener = keyboard.GlobalHotKeys(
            {
                toggle_combo: self._wrap(self._on_toggle),
                quit_combo: self._wrap(self._on_quit) if self._on_quit else self._noop,
            }
        )
        self._listener.start()

    @staticmethod
    def _wrap(fn: Optional[Callable[[], None]]) -> Callable[[], None]:
        def _runner() -> None:
            if fn:
                _post_to_gui(fn)

        return _runner

    @staticmethod
    def _noop() -> None:
        return None

    def stop(self) -> None:
        self._listener.stop()


class CornerWatcher:
    """Polls cursor position and triggers when entering a target corner."""

    def __init__(
        self,
        threshold_px: int,
        position: str,
        on_enter: Callable[[], None],
        screen_name: Optional[str] = None,
    ) -> None:
        self._threshold_px = max(1, threshold_px)
        self._position = position
        self._on_enter = on_enter
        self._target_screen_name = screen_name
        self._enabled = True
        self._in_corner = False
        self._timer = QTimer()
        self._timer.setInterval(75)
        self._timer.timeout.connect(self._poll_cursor)
        self._timer.start()

    def set_threshold(self, threshold_px: int) -> None:
        self._threshold_px = max(1, threshold_px)

    def set_position(self, position: str) -> None:
        self._position = position
        self._in_corner = False

    def set_screen_name(self, screen_name: Optional[str]) -> None:
        self._target_screen_name = screen_name
        self._in_corner = False

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        # Reset corner state when toggling to avoid stale latched value.
        self._in_corner = False

    def _poll_cursor(self) -> None:
        if not self._enabled:
            return
        pos = QCursor.pos()
        x, y = pos.x(), pos.y()
        screens = QGuiApplication.screens()
        if not screens:
            return
        target_screen = None
        if self._target_screen_name:
            for s in screens:
                if s.name() == self._target_screen_name:
                    target_screen = s
                    break
        screens_to_check = [target_screen] if target_screen else screens
        in_any_corner = False
        for screen in screens_to_check:
            geometry = screen.geometry()
            if _is_in_corner(geometry, x, y, self._threshold_px, self._position):
                in_any_corner = True
                break

        if in_any_corner and not self._in_corner:
            self._in_corner = True
            _post_to_gui(self._on_enter)
        elif not in_any_corner and self._in_corner:
            self._in_corner = False

    def stop(self) -> None:
        self._timer.stop()


class CornerIndicator(QWidget):
    """Small always-on-top visual marker for the hot corner."""

    def __init__(self, screen, size: int, position: str) -> None:
        super().__init__()
        self._screen = screen
        self._size = size
        self._position = position
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._label = QLabel("hot corner", self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(
            "color: white; font-size: 10px; font-weight: 600; background: transparent;"
        )
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        self.setFixedSize(self._size, self._size)
        self._label.setFixedSize(self._size, self._size)
        self._label.move(0, 0)
        self._reposition()
        self.setStyleSheet(
            "background-color: rgba(46, 125, 50, 120); border: 2px solid rgba(255,255,255,140);"
        )

    def _reposition(self) -> None:
        g = self._screen.geometry()
        x = g.x()
        y = g.y()
        if self._position == "bottom-left":
            x = g.x()
            y = g.y() + g.height() - self.height()
        elif self._position == "bottom-right":
            x = g.x() + g.width() - self.width()
            y = g.y() + g.height() - self.height()
        elif self._position == "top-left":
            x = g.x()
            y = g.y()
        elif self._position == "top-right":
            x = g.x() + g.width() - self.width()
            y = g.y()
        self.move(x, y)

    def update_size(self, size: int) -> None:
        self._size = size
        self._rebuild_ui()

    def update_position(self, position: str) -> None:
        self._position = position
        self._reposition()

    def show_indicator(self) -> None:
        self._reposition()
        self.show()
        self.raise_()


class CornerIndicatorManager:
    """Manages corner indicators for selected screen (or all if none)."""

    def __init__(self, size: int, position: str, enabled: bool, target_screen: Optional[str]) -> None:
        self._size = size
        self._position = position
        self._enabled = enabled
        self._target_screen = target_screen
        self._indicators: list[CornerIndicator] = []
        self._rebuild()

    def _rebuild(self) -> None:
        for indicator in self._indicators:
            indicator.close()
        screens = QGuiApplication.screens()
        selected = None
        if self._target_screen:
            for s in screens:
                if s.name() == self._target_screen:
                    selected = s
                    break
        if selected:
            screens = [selected]
        self._indicators = [CornerIndicator(screen, self._size, self._position) for screen in screens]
        self._sync_visibility()

    def set_size(self, size: int) -> None:
        self._size = size
        for indicator in self._indicators:
            indicator.update_size(size)
        self._sync_visibility()

    def set_position(self, position: str) -> None:
        self._position = position
        for indicator in self._indicators:
            indicator.update_position(position)
        self._sync_visibility()

    def set_target_screen(self, name: Optional[str]) -> None:
        self._target_screen = name
        self._rebuild()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._sync_visibility()

    def handle_screen_change(self) -> None:
        self._rebuild()

    def close(self) -> None:
        for indicator in self._indicators:
            indicator.close()
        self._indicators = []

    def _sync_visibility(self) -> None:
        for indicator in self._indicators:
            if self._enabled:
                indicator.show_indicator()
            else:
                indicator.hide()


class OverlayWindow(QWidget):
    """Full-screen-ish overlay that captures mouse events and blocks underlying apps."""

    def __init__(
        self,
        on_event: Callable[[str, dict], None],
        on_escape: Callable[[], None],
        size: Optional[QSize] = None,
    ) -> None:
        super().__init__()
        self._event_callback = on_event
        self._escape_callback = on_escape
        self._target_screen: Optional[QScreen] = None
        self.setWindowTitle("Cross Cursors Overlay")
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._info_label = QLabel("Overlay idle")
        self._info_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        self._event_label = QLabel("Move the mouse to start capturing.")
        self._event_label.setStyleSheet("font-size: 14px;")
        self._resolution_label = QLabel("")
        self._resolution_label.setStyleSheet("font-size: 12px; color: #e0e0e0;")

        content = QWidget()
        content.setStyleSheet(
            "background-color: rgba(15, 15, 20, 150); color: white; border-radius: 10px;"
        )
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(8)
        content_layout.addWidget(self._info_label)
        content_layout.addWidget(self._event_label)
        content_layout.addWidget(self._resolution_label)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.addWidget(content, alignment=Qt.AlignTop)

        self._default_size = size or self._screen_size()
        self.set_overlay_size(self._default_size)

    def _screen_size(self) -> QSize:
        screen = self._target_screen or QGuiApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        return geometry.size()

    def set_screen_by_name(self, name: Optional[str]) -> None:
        self._target_screen = self._resolve_screen(name)
        self._center_on_screen()
        self._update_resolution_label()

    def _resolve_screen(self, name: Optional[str]) -> Optional[QScreen]:
        if name:
            for screen in QGuiApplication.screens():
                if screen.name() == name:
                    return screen
        return QGuiApplication.primaryScreen()

    def set_target_screen(self, screen: Optional[QScreen]) -> None:
        self._target_screen = screen
        self._center_on_screen()
        self._update_resolution_label()

    def set_overlay_size(self, size: QSize) -> None:
        self.resize(size)
        self._center_on_screen()
        self._update_resolution_label()

    def _center_on_screen(self) -> None:
        screen = self._target_screen or QGuiApplication.primaryScreen()
        if not screen:
            return
        geometry = screen.availableGeometry()
        x = geometry.x() + (geometry.width() - self.width()) // 2
        y = geometry.y() + (geometry.height() - self.height()) // 2
        self.move(QPoint(x, y))

    def _update_resolution_label(self) -> None:
        self._resolution_label.setText(f"Overlay size: {self.width()} x {self.height()}")

    def show_overlay(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._info_label.setText("Overlay active • mouse input captured")

    def hide_overlay(self) -> None:
        self.hide()
        self._info_label.setText("Overlay idle")

    def _resolve_screen_for_point(self, global_pos: QPoint) -> QScreen:
        screen = self._target_screen
        if screen:
            geo = screen.geometry()
            if geo.contains(global_pos):
                return screen
        detected = QGuiApplication.screenAt(global_pos)
        return detected or self._target_screen or QGuiApplication.primaryScreen()

    def _relative_payload(
        self, global_pos: QPoint, extra: dict
    ) -> tuple[str, dict]:
        screen = self._resolve_screen_for_point(global_pos)
        geo = screen.geometry()
        rel_x = int(global_pos.x() - geo.x())
        rel_y = int(global_pos.y() - geo.y())
        prefix = f"screen {screen.name()} rel ({rel_x}, {rel_y}) • global ({int(global_pos.x())}, {int(global_pos.y())})"
        payload = {
            "screen": screen.name(),
            "x": int(global_pos.x()),
            "y": int(global_pos.y()),
            "x_rel": rel_x,
            "y_rel": rel_y,
            "screen_width": geo.width(),
            "screen_height": geo.height(),
        }
        payload.update(extra)
        return prefix, payload

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position()
        global_pos = event.globalPosition()
        prefix, payload = self._relative_payload(global_pos, {"type": "move"})
        text = f"Move @ overlay ({pos.x():.0f}, {pos.y():.0f}) • {prefix}"
        self._info_label.setText(text)
        self._event_callback(text, payload)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position()
        global_pos = event.globalPosition()
        button = event.button().name if hasattr(event.button(), "name") else str(event.button())
        prefix, payload = self._relative_payload(
            global_pos, {"type": "press", "button": button}
        )
        text = f"Press {button} at overlay ({pos.x():.0f}, {pos.y():.0f}) • {prefix}"
        self._event_label.setText(text)
        self._event_callback(text, payload)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position()
        global_pos = event.globalPosition()
        button = event.button().name if hasattr(event.button(), "name") else str(event.button())
        prefix, payload = self._relative_payload(
            global_pos, {"type": "release", "button": button}
        )
        text = f"Release {button} at overlay ({pos.x():.0f}, {pos.y():.0f}) • {prefix}"
        self._event_label.setText(text)
        self._event_callback(text, payload)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta()
        global_pos = event.globalPosition()
        prefix, payload = self._relative_payload(
            global_pos, {"type": "scroll", "dx": delta.x(), "dy": delta.y()}
        )
        text = f"Wheel delta x={delta.x()} y={delta.y()} • {prefix}"
        self._event_label.setText(text)
        self._event_callback(text, payload)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape:
            self._escape_callback()
            event.accept()
        else:
            super().keyPressEvent(event)


class ControlWindow(QMainWindow):
    """Small control panel to manage the overlay and resolution."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cross Cursors Controller")
        self._config = load_config()
        self._hotkeys: Optional[HotkeyService] = None
        self._corner_watcher: Optional[CornerWatcher] = None
        self._corner_indicators: Optional[CornerIndicatorManager] = None
        self._server: Optional[MouseSocketServer] = None
        self._screen_map: dict[str, QScreen] = {}

        self._refresh_screens()
        default_size = QSize(
            int(self._config.get("overlay_width", self._screen_size().width())),
            int(self._config.get("overlay_height", self._screen_size().height())),
        )
        self._overlay = OverlayWindow(
            on_event=self._handle_overlay_event, on_escape=self._hide_overlay, size=default_size
        )

        self._state_label = QLabel("Overlay OFF")
        self._state_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #c62828;")
        self._last_event_label = QLabel("Last event: none")
        self._last_event_label.setWordWrap(True)
        self._hotkey_hint = QLabel("Global shortcut: Ctrl+Alt+O (toggle) • Ctrl+Alt+Q (quit)")
        self._hotkey_hint.setStyleSheet("color: #666;")

        self._width_spin = QSpinBox()
        self._width_spin.setRange(200, 9999)
        self._width_spin.setValue(default_size.width())
        self._height_spin = QSpinBox()
        self._height_spin.setRange(200, 9999)
        self._height_spin.setValue(default_size.height())

        self._corner_checkbox = QCheckBox("Auto-activate when cursor hits hot corner")
        self._corner_checkbox.setChecked(bool(self._config.get("corner_enabled", True)))
        self._corner_size_spin = QSpinBox()
        self._corner_size_spin.setRange(10, 400)
        self._corner_size_spin.setValue(int(self._config.get("corner_size", 60)))
        self._corner_size_spin.setSuffix(" px")
        self._corner_position_combo = QComboBox()
        self._corner_position_combo.addItems(CORNER_POSITIONS)
        corner_pos = self._config.get("corner_position", "bottom-left")
        if corner_pos not in CORNER_POSITIONS:
            corner_pos = "bottom-left"
        self._corner_position_combo.setCurrentText(corner_pos)

        self._screen_combo = QComboBox()
        self._screen_combo.addItems(self._screen_map.keys())
        saved_screen = self._config.get("overlay_screen", "")
        if saved_screen in self._screen_map:
            self._screen_combo.setCurrentText(saved_screen)
            self._overlay.set_target_screen(self._screen_map[saved_screen])
        else:
            # Default to primary if available
            primary = QGuiApplication.primaryScreen()
            if primary and primary.name() in self._screen_map:
                self._screen_combo.setCurrentText(primary.name())
                self._overlay.set_target_screen(primary)

        self._server_checkbox = QCheckBox("Serve mouse events over TCP")
        self._server_checkbox.setChecked(bool(self._config.get("server_enabled", False)))
        self._server_port_spin = QSpinBox()
        self._server_port_spin.setRange(1024, 65535)
        self._server_port_spin.setValue(int(self._config.get("server_port", 8765)))

        size_grid = QGridLayout()
        size_grid.addWidget(QLabel("Width"), 0, 0)
        size_grid.addWidget(self._width_spin, 0, 1)
        size_grid.addWidget(QLabel("Height"), 1, 0)
        size_grid.addWidget(self._height_spin, 1, 1)
        size_grid.addWidget(QLabel("Corner trigger size"), 2, 0)
        size_grid.addWidget(self._corner_size_spin, 2, 1)
        size_grid.addWidget(QLabel("Corner position"), 3, 0)
        size_grid.addWidget(self._corner_position_combo, 3, 1)
        size_grid.addWidget(QLabel("Overlay screen"), 4, 0)
        size_grid.addWidget(self._screen_combo, 4, 1)
        size_grid.addWidget(QLabel("Server port"), 5, 0)
        size_grid.addWidget(self._server_port_spin, 5, 1)

        apply_size_btn = QPushButton("Apply size")
        apply_size_btn.clicked.connect(self._apply_size)

        toggle_btn = QPushButton("Toggle overlay (Ctrl+Alt+O)")
        toggle_btn.clicked.connect(self._toggle_overlay)

        stop_btn = QPushButton("Quit app (Ctrl+Alt+Q)")
        stop_btn.clicked.connect(self.close)

        button_row = QHBoxLayout()
        button_row.addWidget(apply_size_btn)
        button_row.addWidget(toggle_btn)
        button_row.addWidget(stop_btn)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(self._state_label)
        layout.addWidget(self._last_event_label)
        layout.addWidget(self._hotkey_hint)
        self._cursor_screen_label = QLabel("Cursor screen: detecting...")
        self._cursor_screen_label.setStyleSheet("color: #666;")
        layout.addWidget(self._cursor_screen_label)
        layout.addLayout(size_grid)
        layout.addWidget(self._corner_checkbox)
        layout.addWidget(self._server_checkbox)
        layout.addLayout(button_row)
        layout.addStretch()
        self.setCentralWidget(root)

        self._register_local_shortcuts()
        self._register_global_hotkeys()
        self._register_corner_watcher()
        self._register_corner_indicators()
        self._register_server()
        self._screen_combo.currentTextChanged.connect(self._on_screen_change_selection)
        self._start_cursor_screen_tracker()
        self._apply_size()

    def _screen_size(self) -> QSize:
        screen = QGuiApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        return geometry.size()

    def _register_local_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+Alt+O"), self, activated=self._toggle_overlay)
        QShortcut(QKeySequence("Ctrl+Alt+Q"), self, activated=self.close)

    def _register_global_hotkeys(self) -> None:
        self._hotkeys = HotkeyService(on_toggle=self._toggle_overlay, on_quit=self.close)

    def _register_corner_watcher(self) -> None:
        self._corner_watcher = CornerWatcher(
            threshold_px=self._corner_size_spin.value(),
            position=self._corner_position_combo.currentText(),
            on_enter=self._show_overlay_from_corner,
            screen_name=self._screen_combo.currentText(),
        )
        self._corner_checkbox.toggled.connect(self._on_corner_toggle)
        self._corner_size_spin.valueChanged.connect(self._on_corner_size_change)
        self._corner_position_combo.currentTextChanged.connect(self._on_corner_position_change)
        self._update_corner_state()

    def _register_corner_indicators(self) -> None:
        self._corner_indicators = CornerIndicatorManager(
            size=self._corner_size_spin.value(),
            position=self._corner_position_combo.currentText(),
            enabled=self._corner_checkbox.isChecked(),
            target_screen=self._screen_combo.currentText(),
        )
        self._connect_screen_signals()

    def _register_server(self) -> None:
        self._server_checkbox.toggled.connect(self._on_server_toggle)
        self._server_port_spin.valueChanged.connect(self._on_server_port_change)
        self._update_server_state()

    def _connect_screen_signals(self) -> None:
        app = QGuiApplication.instance()
        if not app:
            return
        app.screenAdded.connect(self._on_screen_change)
        app.screenRemoved.connect(self._on_screen_change)

    def _apply_size(self) -> None:
        size = QSize(self._width_spin.value(), self._height_spin.value())
        self._overlay.set_overlay_size(size)
        self._set_last_event(f"Overlay resized to {size.width()} x {size.height()}")
        self._save_config()

    def _toggle_overlay(self) -> None:
        if self._overlay.isVisible():
            self._hide_overlay()
        else:
            self._show_overlay()

    def _set_overlay_screen(self) -> None:
        screen = self._screen_map.get(self._screen_combo.currentText())
        self._overlay.set_target_screen(screen)

    def _show_overlay(self) -> None:
        self._set_overlay_screen()
        self._overlay.show_overlay()
        self._state_label.setText("Overlay ON")
        self._state_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #2e7d32;")

    def _hide_overlay(self) -> None:
        self._overlay.hide_overlay()
        self._state_label.setText("Overlay OFF")
        self._state_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #c62828;")

    def _start_server(self) -> None:
        try:
            if self._server:
                self._server.stop()
            self._server = MouseSocketServer(port=self._server_port_spin.value())
            self._server.start()
        except OSError as exc:
            self._set_last_event(f"Server failed to start: {exc}")

    def _stop_server(self) -> None:
        if self._server:
            self._server.stop()
            self._server = None

    def _update_server_state(self) -> None:
        if self._server_checkbox.isChecked():
            self._start_server()
        else:
            self._stop_server()
        self._save_config()

    def _set_last_event(self, message: str) -> None:
        self._last_event_label.setText(f"Last event: {message}")
        # Also log to stdout for external piping/forwarding if desired.
        print(message)
        self._update_cursor_screen_label()

    def _handle_overlay_event(self, message: str, payload: dict) -> None:
        self._set_last_event(message)
        if self._server and self._server_checkbox.isChecked():
            enriched = {
                **payload,
                "overlay_width": self._overlay.width(),
                "overlay_height": self._overlay.height(),
            }
            self._server.broadcast(enriched)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._hotkeys:
            self._hotkeys.stop()
        if self._corner_watcher:
            self._corner_watcher.stop()
        if self._corner_indicators:
            self._corner_indicators.close()
        if self._server:
            self._server.stop()
        self._save_config()
        self._overlay.hide()
        super().closeEvent(event)

    def _on_corner_toggle(self, checked: bool) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_enabled(checked)
        if self._corner_indicators:
            self._corner_indicators.set_enabled(checked)
        self._save_config()

    def _on_corner_size_change(self, value: int) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_threshold(value)
        if self._corner_indicators:
            self._corner_indicators.set_size(value)
        self._save_config()

    def _on_corner_position_change(self, value: str) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_position(value)
        if self._corner_indicators:
            self._corner_indicators.set_position(value)
        self._save_config()

    def _on_screen_change_selection(self, _value: str) -> None:
        self._set_overlay_screen()
        if self._corner_watcher:
            self._corner_watcher.set_screen_name(self._screen_combo.currentText())
        if self._corner_indicators:
            self._corner_indicators.set_target_screen(self._screen_combo.currentText())
        # Optionally adapt size to selected screen if it would exceed the screen.
        screen = self._screen_map.get(self._screen_combo.currentText())
        if screen:
            geo = screen.availableGeometry()
            if self._width_spin.value() > geo.width() or self._height_spin.value() > geo.height():
                self._width_spin.setValue(min(self._width_spin.value(), geo.width()))
                self._height_spin.setValue(min(self._height_spin.value(), geo.height()))
                self._apply_size()
        self._save_config()

    def _on_server_toggle(self, _checked: bool) -> None:
        self._update_server_state()

    def _on_server_port_change(self, _value: int) -> None:
        if self._server_checkbox.isChecked():
            self._start_server()
        self._save_config()

    def _update_corner_state(self) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_threshold(self._corner_size_spin.value())
            self._corner_watcher.set_enabled(self._corner_checkbox.isChecked())
            self._corner_watcher.set_position(self._corner_position_combo.currentText())
            self._corner_watcher.set_screen_name(self._screen_combo.currentText())
        if self._corner_indicators:
            self._corner_indicators.set_size(self._corner_size_spin.value())
            self._corner_indicators.set_enabled(self._corner_checkbox.isChecked())
            self._corner_indicators.set_position(self._corner_position_combo.currentText())
            self._corner_indicators.set_target_screen(self._screen_combo.currentText())
        self._save_config()

    def _start_cursor_screen_tracker(self) -> None:
        self._cursor_screen_timer = QTimer(self)
        self._cursor_screen_timer.setInterval(200)
        self._cursor_screen_timer.timeout.connect(self._update_cursor_screen_label)
        self._cursor_screen_timer.start()
        self._update_cursor_screen_label()

    def _update_cursor_screen_label(self) -> None:
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos)
        name = screen.name() if screen else "unknown"
        self._cursor_screen_label.setText(f"Cursor screen: {name}")

    def _show_overlay_from_corner(self) -> None:
        if not self._overlay.isVisible():
            self._show_overlay()
            self._set_last_event("Auto-activated from hot corner trigger")

    def _on_screen_change(self, _screen) -> None:
        self._refresh_screens()
        current = self._screen_combo.currentText()
        self._screen_combo.blockSignals(True)
        self._screen_combo.clear()
        self._screen_combo.addItems(self._screen_map.keys())
        if current in self._screen_map:
            self._screen_combo.setCurrentText(current)
        self._screen_combo.blockSignals(False)
        if self._corner_indicators:
            self._corner_indicators.handle_screen_change()
        # Re-apply screen target for overlay after hotplug changes.
        self._set_overlay_screen()
        if self._corner_watcher:
            self._corner_watcher.set_screen_name(self._screen_combo.currentText())
        if self._corner_indicators:
            self._corner_indicators.set_target_screen(self._screen_combo.currentText())
        # Update cursor-screen tracking label immediately.
        self._update_cursor_screen_label()

    def _refresh_screens(self) -> None:
        self._screen_map = {s.name(): s for s in QGuiApplication.screens()}
        if not self._screen_map:
            primary = QGuiApplication.primaryScreen()
            if primary:
                self._screen_map[primary.name()] = primary
        # Keep the screen dropdown in sync with current map.
        current = self._screen_combo.currentText() if hasattr(self, "_screen_combo") else ""
        if hasattr(self, "_screen_combo"):
            self._screen_combo.blockSignals(True)
            self._screen_combo.clear()
            self._screen_combo.addItems(self._screen_map.keys())
            if current in self._screen_map:
                self._screen_combo.setCurrentText(current)
            self._screen_combo.blockSignals(False)

    def _save_config(self) -> None:
        self._config = {
            "overlay_width": self._width_spin.value(),
            "overlay_height": self._height_spin.value(),
            "overlay_screen": self._screen_combo.currentText(),
            "corner_enabled": self._corner_checkbox.isChecked(),
            "corner_size": self._corner_size_spin.value(),
            "corner_position": self._corner_position_combo.currentText(),
            "server_enabled": self._server_checkbox.isChecked(),
            "server_port": self._server_port_spin.value(),
        }
        save_config(self._config)


def main() -> None:
    app = QApplication(sys.argv)
    controller = ControlWindow()
    controller.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
